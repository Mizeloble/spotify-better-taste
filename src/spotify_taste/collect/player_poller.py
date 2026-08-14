"""무엇/왜
주 수집기. `GET /me/player` 를 25초마다 찔러 `progress_ms` 궤적을 모은다.
`ms_played` 를 실측할 수 있는 **유일한** 경로이고, 이 프로젝트의 선호 신호가
전부 여기서 나온다.

설계 원칙
- **저장이 먼저, 해석은 그 다음.** 한 틱은 (원본 아카이브 → `player_sample`
  적재 → 상태머신 투입 → 세션 upsert) 순서이고 통째로 한 트랜잭션이다.
  해석이 실패해도 원시 데이터는 남아야 나중에 재계산할 수 있다.
- **204 는 정상이다.** 유휴 백오프는 오류 백오프와 **별도 카운터**를 쓴다.
  오류를 유휴로 착각하면 활발한 청취 중에 조용히 폴링이 느려진다.
- **유휴 백오프 상한은 120초.** 300초까지 늘리면 청취를 재개했을 때 최대
  5분을 `ms_played` 가 없는 recently-played 경로에 넘기게 된다.
- **슬롯을 못 얻으면 틱을 건너뛴다.** 백필 뒤에 줄서서 낡은 샘플을 쌓느니
  공백을 기록하고 넘어가는 편이 낫다.
- **heartbeat 는 샘플 적재에 성공한 뒤에만 찍는다.** 살아 있음의 정의가
  "루프를 돌았다"가 아니라 "수집했다"여야 한다.

어떻게 확인
    uv run spotify-taste collect --once
    uv run pytest tests/test_player_poller.py
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import structlog

from .._common import now_ms
from ..config import Settings, get_settings
from ..db.connection import tx
from ..sessions.machine import SessionMachine
from ..sessions.model import PlayerSample, SampleKind
from ..sessions.store import (
    finalize_open_session,
    insert_gap,
    insert_player_sample,
    load_open_sessions,
    upsert_closed_session,
    upsert_open_session,
)
from ..spotify.client import SpotifyApiError, SpotifyClient
from ..spotify.endpoints import Endpoint
from ..spotify.raw_archive import RawArchive
from ..spotify.schemas import parse_player_sample

__all__ = ["PlayerPoller", "TickOutcome"]

log = structlog.get_logger(__name__)

ERROR_BACKOFF_SEC = (5, 10, 20, 40, 60)
#: 이보다 어린 반열림 세션은 재시작을 견디고 이어진다.
RESUME_WINDOW_MS = 120_000
#: 슬롯을 이 시간 안에 못 얻으면 틱을 포기한다.
SLOT_TIMEOUT_SEC = 5.0
#: 대기가 의도보다 이만큼 넘게 길어졌으면 호스트가 잤다 온 것이다.
#: 이벤트 루프 지연은 초 단위, 슬립은 분~시간 단위라 5분이면 안전하게 갈린다.
HOST_SLEEP_JUMP_MS = 300_000


class TickOutcome(StrEnum):
    """틱 하나의 결과. 다음 대기 시간을 정하는 데 쓰인다."""

    PLAYING = "playing"
    IDLE = "idle"
    ERROR = "error"
    SKIPPED = "skipped"  # 레이트리밋으로 건너뜀


@dataclass
class PollerState:
    """백오프 인덱스. 유휴와 오류를 절대 섞지 않는다."""

    idle_index: int = 0
    error_index: int = 0


class PlayerPoller:
    """`/me/player` 폴링 루프."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        client: SpotifyClient,
        archive: RawArchive,
        settings: Settings | None = None,
        machine: SessionMachine | None = None,
        on_sample_stored: Callable[[], None] | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._archive = archive
        self._settings = settings or get_settings()
        self._machine = machine or SessionMachine()
        # heartbeat 훅. 샘플 적재에 성공한 뒤에만 불린다.
        self._on_sample_stored = on_sample_stored
        self._state = PollerState()
        self._last_tick_at: int | None = None

    @property
    def machine(self) -> SessionMachine:
        return self._machine

    # --- 재시작 복구 ------------------------------------------------------

    def recover(self, *, now: int | None = None) -> None:
        """반열림 세션을 이어받거나 닫는다. 프로세스 기동 시 1회.

        어느 쪽이든 `collector_gap` 을 남긴다 — 그 구간에 무슨 일이 있었는지
        모르는 것과, 모른다는 사실을 기록하는 것은 다르다.
        """
        current = now if now is not None else now_ms()
        with tx(self._conn):
            for session_id, open_session in load_open_sessions(self._conn):
                age = current - open_session.last_sample_at
                if age <= RESUME_WINDOW_MS:
                    self._machine.rehydrate(open_session)
                    log.info(
                        "poller.session_resumed",
                        track_uri=open_session.track_uri,
                        age_ms=age,
                    )
                else:
                    finalize_open_session(self._conn, session_id, end_reason="process_restart")
                    log.info(
                        "poller.session_abandoned",
                        track_uri=open_session.track_uri,
                        age_ms=age,
                    )
                insert_gap(
                    self._conn,
                    started_at=open_session.last_sample_at,
                    ended_at=current,
                    reason="process_restart",
                )

    # --- 한 틱 -----------------------------------------------------------

    async def tick(self) -> TickOutcome:
        """샘플 한 번. 예외를 밖으로 내지 않는다 (인증 폐기는 예외)."""
        sampled_at = now_ms()
        try:
            resp = await self._client.get(
                Endpoint.PLAYER,
                role="poller",
                source_job="poller",
                acquire_timeout=SLOT_TIMEOUT_SEC,
                ok_statuses=frozenset({200, 204}),
            )
        except SpotifyApiError as exc:
            return self._handle_error(sampled_at, reason=f"api:{exc.status}")

        if resp is None:
            # 레이트리밋으로 슬롯을 못 얻었다. recently-played 가 메꾼다.
            with tx(self._conn):
                insert_gap(
                    self._conn,
                    started_at=self._last_tick_at or sampled_at,
                    ended_at=sampled_at,
                    reason="rate_limit",
                )
            self._last_tick_at = sampled_at
            return TickOutcome.SKIPPED

        sample = parse_player_sample(resp.json, sampled_at=sampled_at, http_status=resp.status)
        self._persist(sample, resp)
        self._last_tick_at = sampled_at

        if sample.kind is SampleKind.IDLE:
            self._state.idle_index += 1
            self._state.error_index = 0
            return TickOutcome.IDLE

        # 재생 샘플 하나면 간격이 즉시 기본값으로 돌아온다.
        self._state.idle_index = 0
        self._state.error_index = 0
        return TickOutcome.PLAYING

    def _persist(self, sample: PlayerSample, resp) -> None:
        """한 틱을 통째로 저장한다. 부분 기록은 재구성을 조용히 망가뜨린다."""
        # 세션의 첫 샘플인지 — 원본을 반드시 남길지 판단하는 기준.
        open_session = self._machine.open_session
        is_session_start = open_session is None or (
            sample.track_uri is not None and sample.track_uri != open_session.track_uri
        )

        with tx(self._conn):
            raw_id = self._archive.store(resp, "poller", force=is_session_start)
            sample_id = insert_player_sample(self._conn, sample, raw_id=raw_id)
            # 상태머신이 샘플 id 를 알아야 세션이 근거를 가리킬 수 있다.
            stamped = PlayerSample(**{**sample.__dict__, "id": sample_id})

            for closed in self._machine.on_sample(stamped):
                upsert_closed_session(self._conn, closed)
            if self._machine.open_session is not None:
                upsert_open_session(self._conn, self._machine.open_session)

        # 적재가 실제로 성공한 뒤에만 살아 있다고 말한다.
        if self._on_sample_stored is not None:
            self._on_sample_stored()

    def _handle_error(self, sampled_at: int, *, reason: str) -> TickOutcome:
        """전송 실패. 세션은 열어 둔 채 아무것도 적립하지 않는다.

        유휴 인덱스는 건드리지 않는다 — 오류는 "재생 중인가"를 알려 주지
        않으므로, 간격을 되돌릴 근거는 실제 재생 샘플뿐이다.
        """
        error_sample = PlayerSample.error(sampled_at)
        with tx(self._conn):
            insert_player_sample(self._conn, error_sample, raw_id=None)
            self._machine.on_sample(error_sample)
            insert_gap(
                self._conn,
                started_at=self._last_tick_at or sampled_at,
                ended_at=sampled_at,
                reason="error_backoff",
            )
        self._last_tick_at = sampled_at
        self._state.error_index += 1
        log.warning("poller.tick_failed", reason=reason, consecutive=self._state.error_index)
        return TickOutcome.ERROR

    # --- 대기 ------------------------------------------------------------

    def next_delay_sec(self, outcome: TickOutcome) -> float:
        """다음 틱까지 기다릴 시간."""
        if outcome is TickOutcome.ERROR:
            idx = min(self._state.error_index - 1, len(ERROR_BACKOFF_SEC) - 1)
            return float(ERROR_BACKOFF_SEC[max(0, idx)])
        if outcome is TickOutcome.IDLE:
            steps = self._settings.idle_backoff_steps
            idx = min(self._state.idle_index - 1, len(steps) - 1)
            return float(steps[max(0, idx)])
        return float(self._settings.player_poll_sec)

    async def run(self, stop: asyncio.Event) -> None:
        """중지 신호가 올 때까지 계속 폴링한다."""
        self.recover()
        while not stop.is_set():
            try:
                outcome = await self.tick()
            except sqlite3.OperationalError as exc:
                # 락 경합으로 한 틱을 잃는 것은 괜찮다. 프로세스가 죽으면
                # launchd 가 재시작할 때까지 수집이 통째로 멈춘다 — 2026-08-13
                # 새벽 백필과 겹쳐 실제로 그렇게 죽었다. tx() 가 롤백했으므로
                # 반쪽 기록은 남지 않는다.
                self._state.error_index += 1
                log.warning(
                    "poller.tick_failed",
                    reason="db_error",
                    error=str(exc),
                    consecutive=self._state.error_index,
                )
                outcome = TickOutcome.ERROR
            delay = self.next_delay_sec(outcome)
            waited_from = now_ms()
            try:
                # 대기 중에도 중지 신호에 즉시 반응한다.
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                # 대기가 의도보다 훨씬 길었다면 그동안 프로세스가 정지돼
                # 있었던 것이다(맥 슬립). 크래시 공백과 뜻이 전혀 다르므로
                # 사유를 나눠 기록한다 — 소크 판정은 host_sleep 을 제외한다.
                jump_ms = now_ms() - waited_from - int(delay * 1000)
                if jump_ms >= HOST_SLEEP_JUMP_MS:
                    try:
                        with tx(self._conn):
                            insert_gap(
                                self._conn,
                                started_at=waited_from,
                                ended_at=now_ms(),
                                reason="host_sleep",
                            )
                        log.info("poller.host_sleep_detected", minutes=jump_ms // 60_000)
                    except sqlite3.OperationalError as exc:
                        # 공백 기록을 잃을지언정 루프는 살아야 한다.
                        log.warning("poller.gap_record_failed", error=str(exc))
                continue
        # 정상 종료: 열린 세션을 닫아 둔다.
        with tx(self._conn):
            for closed in self._machine.close(end_reason="shutdown", ended_at=now_ms()):
                upsert_closed_session(self._conn, closed)
