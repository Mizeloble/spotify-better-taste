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
- **호스트 슬립은 대기와 틱 양쪽에서 잰다.** 맥은 대기 중에만 잠들지 않는다.
  틱 하나가 상한(`TICK_BUDGET_MS`)보다 시간 단위로 길어졌다면 요청·재시도
  백오프 도중에 잠든 것이고, 그 구간도 `host_sleep` 공백이다.

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
    last_sample_at,
    load_open_sessions,
    upsert_closed_session,
    upsert_open_session,
)
from ..spotify.auth import SpotifyAuthError, SpotifyAuthRevoked
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
#: 깨어 있는 틱 하나가 걸릴 수 있는 최대 시간. 슬롯 대기 5초 x 4(최초 +
#: 429 재획득 3회) + 요청 5회(최초·재시도 3회·401 재시도 1회, httpx 단계별
#: 20초라 회당 40초로 잡음) + 토큰 갱신 15초 x 2 + 백오프 3.5초 ≈ 254초.
#: 틱은 이 상한을 "의도한 시간"으로 보고, 대기와 같은 `HOST_SLEEP_JUMP_MS`
#: 기준으로 슬립을 가른다 — 평소 틱은 0.1초라 넉넉하다.
TICK_BUDGET_MS = 300_000


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
        """반열림 세션을 이어받거나 닫고, 멈춰 있던 구간을 공백으로 남긴다.
        프로세스 기동 시 1회.

        공백은 **열린 세션이 있든 없든** 남긴다 — 그 구간에 무슨 일이 있었는지
        모르는 것과, 모른다는 사실을 기록하는 것은 다르다.

        예전에는 반열림 세션이 있을 때만 남겼다. 그래서 아무것도 틀지 않는
        상태(204 유휴)에서 멈추면 닫을 세션이 없어 **멈췄다는 사실 자체가**
        사라졌다. 2026-08-24 18:10 ~ 08-31 23:30, 7일 5시간이 그렇게 통째로
        증발했고 daily-report 는 그 다음 실행에서 "수집 공백 0분" 을 찍었다.
        루프 안의 host_sleep 감지(`run` 참조)는 같은 프로세스가 살아남아야
        동작하므로 이 구멍을 메우지 못한다 — 그때 파이썬 자식은 새로 떴다.

        기준점은 "마지막으로 수집하고 있었음을 아는 시각", 즉 마지막 샘플과
        반열림 세션의 마지막 샘플 중 나중 쪽이다. 샘플이 하나도 없으면
        (최초 기동) 공백이 아니므로 아무것도 남기지 않는다.
        """
        current = now if now is not None else now_ms()
        with tx(self._conn):
            alive_at = last_sample_at(self._conn)
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
                # 샘플 적재와 세션 갱신은 같은 트랜잭션이라 보통 같은 시각이지만,
                # 어느 쪽이 나중이든 "살아 있던 마지막 순간"을 택한다.
                alive_at = max(alive_at or 0, open_session.last_sample_at)
            if alive_at is not None:
                insert_gap(
                    self._conn,
                    started_at=alive_at,
                    ended_at=current,
                    reason="process_restart",
                )

    # --- 한 틱 -----------------------------------------------------------

    async def tick(self) -> TickOutcome:
        """샘플 한 번. 예외를 밖으로 내지 않는다 (인증 폐기는 예외).

        시각은 둘을 잰다. `requested_at` 은 요청을 내기 직전, `received_at` 은
        결과를 손에 쥔 순간이다. 샘플의 `sampled_at` 은 **수신 시각**이다
        (스키마 주석 그대로) — 예전엔 요청 직전 시각을 찍어서, 재시도 도중
        잠들었다 깨어난 틱의 10-04 응답이 09-27 샘플로 들어갔다(아래 참조).

        둘의 간격이 `TICK_BUDGET_MS` 를 시간 단위로 넘으면 틱 도중에 호스트가
        잤던 것이다. 그 구간은 같은 트랜잭션 안에서 `host_sleep` 으로 남기고,
        오류·스킵 공백은 요청 직전에서 끊어 슬립을 오류로 부풀리지 않는다.
        2026-09-27 09:46 KST, 기동 직후 ConnectError 재시도 백오프 중에 맥이
        잠들어 10-04 23:33 에 깼다. 대기 구간만 재던 감지가 이걸 놓쳐 7.5일
        동안 공백이 하나도 남지 않았고, daily-report 는 "수집 공백 0분" 을 찍었다.
        """
        requested_at = now_ms()
        try:
            resp = await self._client.get(
                Endpoint.PLAYER,
                role="poller",
                source_job="poller",
                acquire_timeout=SLOT_TIMEOUT_SEC,
                ok_statuses=frozenset({200, 204}),
            )
        except SpotifyApiError as exc:
            return self._handle_error(requested_at, now_ms(), reason=f"api:{exc.status}")
        except SpotifyAuthRevoked:
            # 사람이 재인증해야만 풀린다. 삼키면 아무도 부르지 못한다.
            raise
        except SpotifyAuthError:
            # 토큰 갱신의 일시적 실패(네트워크·5xx). API 호출 쪽 전송 오류와
            # 뜻이 같은데 그쪽만 흡수되고 있었다 — 갱신은 `request()` 의
            # 재시도 루프 **밖**에서 일어나기 때문이다. 2026-08-22 11:55~21:15,
            # 이게 밖으로 새어 TaskGroup 을 무너뜨려 9시간 20분 동안 558회
            # 재시작만 반복했다. 원인은 그냥 네트워크 단절이었다.
            # 구체적 사유는 auth 쪽이 `auth.refresh_*` 로 이미 남긴다.
            return self._handle_error(requested_at, now_ms(), reason="auth")
        received_at = now_ms()

        if resp is None:
            # 레이트리밋으로 슬롯을 못 얻었다. recently-played 가 메꾼다.
            with tx(self._conn):
                self._insert_tick_gaps(requested_at, received_at, reason="rate_limit")
            self._last_tick_at = received_at
            return TickOutcome.SKIPPED

        sample = parse_player_sample(resp.json, sampled_at=received_at, http_status=resp.status)
        self._persist(sample, resp, requested_at=requested_at)
        self._last_tick_at = received_at

        if sample.kind is SampleKind.IDLE:
            self._state.idle_index += 1
            self._state.error_index = 0
            return TickOutcome.IDLE

        # 재생 샘플 하나면 간격이 즉시 기본값으로 돌아온다.
        self._state.idle_index = 0
        self._state.error_index = 0
        return TickOutcome.PLAYING

    def _persist(self, sample: PlayerSample, resp, *, requested_at: int) -> None:
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
            self._insert_tick_gaps(requested_at, sample.sampled_at, reason=None)

        # 적재가 실제로 성공한 뒤에만 살아 있다고 말한다.
        if self._on_sample_stored is not None:
            self._on_sample_stored()

    def _handle_error(self, requested_at: int, received_at: int, *, reason: str) -> TickOutcome:
        """전송 실패. 세션은 열어 둔 채 아무것도 적립하지 않는다.

        유휴 인덱스는 건드리지 않는다 — 오류는 "재생 중인가"를 알려 주지
        않으므로, 간격을 되돌릴 근거는 실제 재생 샘플뿐이다.
        """
        error_sample = PlayerSample.error(received_at)
        with tx(self._conn):
            insert_player_sample(self._conn, error_sample, raw_id=None)
            self._machine.on_sample(error_sample)
            self._insert_tick_gaps(requested_at, received_at, reason="error_backoff")
        self._last_tick_at = received_at
        self._state.error_index += 1
        log.warning("poller.tick_failed", reason=reason, consecutive=self._state.error_index)
        return TickOutcome.ERROR

    def _insert_tick_gaps(self, requested_at: int, received_at: int, *, reason: str | None) -> None:
        """틱 하나가 남길 공백들. 호출부의 트랜잭션 안에서 부른다.

        `reason` 은 이 틱이 수집에 실패한 사유(None 이면 성공). 실패 공백은
        직전 틱부터 이번 틱까지를 덮되, 틱 도중에 잤다면 요청 직전에서 끊고
        나머지를 `host_sleep` 으로 넘긴다 — 소크 판정은 host_sleep 만 빼고 보므로
        7일 슬립이 error_backoff 로 섞여 들어가면 안 된다.
        """
        slept = _is_host_sleep(received_at - requested_at, expected_ms=TICK_BUDGET_MS)
        if reason is not None:
            insert_gap(
                self._conn,
                started_at=self._last_tick_at or requested_at,
                ended_at=requested_at if slept else received_at,
                reason=reason,
            )
        if slept:
            # 요청을 낸 순간까지는 깨어서 돌고 있었음을 안다 — 직전 샘플 이후는
            # 정상 대기였고, 그 대기는 `run` 이 따로 쟀다.
            insert_gap(
                self._conn, started_at=requested_at, ended_at=received_at, reason="host_sleep"
            )
            log.info(
                "poller.host_sleep_detected",
                where="tick",
                minutes=(received_at - requested_at) // 60_000,
            )

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
            tick_started = now_ms()
            try:
                outcome = await self.tick()
            except sqlite3.OperationalError as exc:
                # 락 경합으로 한 틱을 잃는 것은 괜찮다. 프로세스가 죽으면
                # launchd 가 재시작할 때까지 수집이 통째로 멈춘다 — 2026-08-13
                # 새벽 백필과 겹쳐 실제로 그렇게 죽었다. tx() 가 롤백했으므로
                # 반쪽 기록은 남지 않는다 — 틱이 남기려던 host_sleep 공백까지
                # 같이 사라졌을 수 있으니 여기서 다시 잰다.
                tick_ended = now_ms()
                if _is_host_sleep(tick_ended - tick_started, expected_ms=TICK_BUDGET_MS):
                    self._record_host_sleep(tick_started, tick_ended, where="tick")
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
                woke_at = now_ms()
                if _is_host_sleep(woke_at - waited_from, expected_ms=int(delay * 1000)):
                    self._record_host_sleep(waited_from, woke_at, where="wait")
                continue
        # 정상 종료: 열린 세션을 닫아 둔다.
        with tx(self._conn):
            for closed in self._machine.close(end_reason="shutdown", ended_at=now_ms()):
                upsert_closed_session(self._conn, closed)

    def _record_host_sleep(self, started_at: int, ended_at: int, *, where: str) -> None:
        """틱 밖에서 잰 슬립 공백. 자기 트랜잭션으로 남긴다."""
        try:
            with tx(self._conn):
                insert_gap(
                    self._conn, started_at=started_at, ended_at=ended_at, reason="host_sleep"
                )
            log.info(
                "poller.host_sleep_detected", where=where, minutes=(ended_at - started_at) // 60_000
            )
        except sqlite3.OperationalError as exc:
            # 공백 기록을 잃을지언정 루프는 살아야 한다.
            log.warning("poller.gap_record_failed", error=str(exc))


def _is_host_sleep(elapsed_ms: int, *, expected_ms: int) -> bool:
    """벽시계 경과가 의도한 시간보다 `HOST_SLEEP_JUMP_MS` 이상 길었는가."""
    return elapsed_ms - expected_ms >= HOST_SLEEP_JUMP_MS
