"""무엇/왜
`progress_ms` 궤적을 재생 세션으로 바꾸는 상태머신. 이 프로젝트에서 가장
값진 코드이자, 유일하게 "틀리면 데이터 전체가 무의미해지는" 부분이다.

설계 원칙
- **완전히 순수하다. DB 도 HTTP 도 벽시계도 없다.** 시간은 샘플의
  `sampled_at` 에서만 온다. 그래서 실시간 폴러와 `sessions rebuild` 가 같은
  코드를 탈 수 있고, 둘의 출력이 같다는 것을 테스트로 단언할 수 있다.
  이것이 "수집과 해석의 분리"가 운영적으로 뜻하는 바다.
- **의심스러우면 세션을 나눈다.** 되감기와 반복 재생은 둘 다 큰 음수 `dp` 로
  나타나고, 둘 다 새 세션이 맞다. 합치면 한 곡을 5번 반복한 것이 한 번의
  긴 재생으로 뭉개진다.
- **적립은 관측한 만큼만.** 앞으로 탐색한 구간은 `ms_played` 에 넣지 않는다.
  듣지 않은 시간이다.
- **스킵은 버리지 않는다.** 8초 만에 다음 곡으로 넘긴 기록은 이 데이터셋에서
  가장 값진 negative 라벨이다.

어떻게 확인
    uv run pytest tests/test_session_machine.py
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .model import OpenSession, PlayerSample, PlaySession, SampleKind

__all__ = ["MachineConfig", "SessionMachine"]

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MachineConfig:
    """판정 상수. 값을 바꾸면 `sessions rebuild` 로 과거를 다시 계산할 수 있다."""

    #: 시계 지터를 넘는 progress 감소 = 되감기.
    rewind_threshold_ms: int = 3_000
    #: 벽시계 경과보다 이만큼까지 더 나아간 것은 오차로 본다.
    seek_tolerance_ms: int = 2_000
    #: 이만큼 progress 가 멈춰 있으면 세션을 닫는다 (30분).
    pause_timeout_ms: int = 1_800_000
    #: 이보다 짧으면 재생이 아니라 스크럽 아티팩트로 본다.
    min_emit_ms: int = 1_000


class SessionMachine:
    """샘플을 하나씩 먹여 종료된 세션을 받아 낸다."""

    def __init__(self, config: MachineConfig | None = None) -> None:
        self.config = config or MachineConfig()
        self._open: OpenSession | None = None

    @property
    def open_session(self) -> OpenSession | None:
        """진행 중인 세션. 폴러가 매 틱 `is_open=1` 로 upsert 한다."""
        return self._open

    def rehydrate(self, session: OpenSession) -> None:
        """재시작 후 반열림 세션을 이어받는다."""
        self._open = session

    # --- 핵심 -------------------------------------------------------------

    def on_sample(self, s: PlayerSample) -> list[PlaySession]:
        """샘플 하나를 처리하고, 이번에 종료된 세션들을 돌려준다.

        보통 0개나 1개다. 리스트인 이유는 호출부가 분기 없이 쓸 수 있게 하려고.
        """
        if s.kind is SampleKind.ERROR:
            # 아무것도 알 수 없는 구간이다. 세션은 열어 둔 채 아무것도
            # 적립하지 않는다 — 오류 시간을 청취 시간으로 세면 안 된다.
            return []

        if s.kind is SampleKind.IDLE:
            return self._on_idle(s)

        if s.track_uri is None or s.progress_ms is None:
            # 200 이지만 트랙 정보가 없다 (광고, 팟캐스트, 알 수 없는 타입).
            # 유휴와 같게 다룬다.
            return self._on_idle(s)

        return self._on_playing(s)

    def close(self, *, end_reason: str, ended_at: int) -> list[PlaySession]:
        """열린 세션을 강제로 닫는다 (프로세스 종료, 재시작 복구)."""
        if self._open is None:
            return []
        emitted = self._emit(self._open, end_reason=end_reason, ended_at=ended_at)
        self._open = None
        return emitted

    # --- 분기 -------------------------------------------------------------

    def _on_idle(self, s: PlayerSample) -> list[PlaySession]:
        """204 또는 재생 항목 없음.

        "일시정지"와 "정지"를 구별할 방법이 없으므로 일시정지로 가정한다.
        충분히 오래 지속되면 그때 닫는다.
        """
        if self._open is None:
            return []
        self._open.is_paused = True
        if s.sampled_at - self._open.last_progress_change_at > self.config.pause_timeout_ms:
            emitted = self._emit(
                self._open,
                end_reason="idle_timeout",
                # 마지막으로 실제 재생을 본 시점까지만 세션으로 친다.
                ended_at=self._open.last_sample_at,
            )
            self._open = None
            return emitted
        return []

    def _on_playing(self, s: PlayerSample) -> list[PlaySession]:
        assert s.track_uri is not None and s.progress_ms is not None

        if self._open is None:
            if s.is_playing is False:
                # 일시정지 상태의 트랙으로는 세션을 열지 않는다. 멈춰 있는 것은
                # 청취가 아니고, pause_timeout 으로 방금 닫은 세션이 곧바로
                # 되살아나는 것도 막는다. 재생이 재개되면 그때 새로 연다.
                return []
            self._open = _start(s)
            return []

        if s.track_uri != self._open.track_uri:
            emitted = self._emit(self._open, end_reason="track_change", ended_at=s.sampled_at)
            self._open = _start(s)
            return emitted

        open_ = self._open
        dp = s.progress_ms - open_.last_progress_ms
        dt = s.sampled_at - open_.last_sample_at

        if dp < -self.config.rewind_threshold_ms:
            # 되감기 또는 0에서 재시작. 둘 다 새 세션이 맞다 — 합치면 한 곡을
            # 5번 반복한 것이 한 번의 긴 재생으로 뭉개진다.
            emitted = self._emit(open_, end_reason="rewind", ended_at=s.sampled_at)
            self._open = _start(s)
            return emitted

        # duration 은 relinking 으로 재생 도중 바뀔 수 있다. 최신 값을 쓴다.
        if s.duration_ms:
            open_.duration_ms = s.duration_ms

        if s.is_playing is False:
            return self._on_paused(open_, s, dp=dp, dt=dt)

        return self._on_progressing(open_, s, dp=dp, dt=dt)

    def _on_paused(
        self, open_: OpenSession, s: PlayerSample, *, dp: int, dt: int
    ) -> list[PlaySession]:
        assert s.progress_ms is not None
        # 일시정지 상태로 관측됐어도 직전 구간은 재생됐을 수 있다.
        # 벽시계 경과보다 많이 적립하지 않는다.
        open_.ms_played_accum += max(0, min(dp, dt))
        open_.max_progress_ms = max(open_.max_progress_ms, s.progress_ms)
        open_.is_paused = True
        if dp != 0:
            open_.last_progress_change_at = s.sampled_at
        open_.last_progress_ms = s.progress_ms
        open_.last_sample_at = s.sampled_at
        open_.sample_count += 1
        open_.last_sample_id = s.id

        if s.sampled_at - open_.last_progress_change_at > self.config.pause_timeout_ms:
            emitted = self._emit(
                open_,
                end_reason="pause_timeout",
                ended_at=open_.last_progress_change_at,
            )
            self._open = None
            return emitted
        return []

    def _on_progressing(
        self, open_: OpenSession, s: PlayerSample, *, dp: int, dt: int
    ) -> list[PlaySession]:
        assert s.progress_ms is not None
        if dp > dt + self.config.seek_tolerance_ms:
            # 앞으로 탐색. 건너뛴 구간은 듣지 않았으므로 적립하지 않는다.
            open_.seek_forward_count += 1
            credit = 0
        else:
            credit = max(0, dp)

        open_.ms_played_accum += credit
        open_.max_progress_ms = max(open_.max_progress_ms, s.progress_ms)
        open_.is_paused = False
        if dp != 0:
            open_.last_progress_change_at = s.sampled_at
        open_.last_progress_ms = s.progress_ms
        open_.last_sample_at = s.sampled_at
        open_.sample_count += 1
        open_.last_sample_id = s.id
        # 재생 중에도 컨텍스트가 뒤늦게 채워지는 계정이 있다.
        if open_.context_uri is None and s.context_uri is not None:
            open_.context_uri = s.context_uri
            open_.context_type = s.context_type
        return []

    # --- 종료 -------------------------------------------------------------

    def _emit(self, o: OpenSession, *, end_reason: str, ended_at: int) -> list[PlaySession]:
        ms_played = o.ms_played_accum
        duration = o.duration_ms or None
        completion = min(1.0, o.max_progress_ms / duration) if duration else None
        listened = min(1.0, ms_played / duration) if duration else None

        # 스크럽 아티팩트는 버리되, **스킵은 절대 버리지 않는다.**
        # 8초 만에 넘긴 기록이 이 데이터셋에서 가장 값진 negative 라벨이다.
        if ms_played < self.config.min_emit_ms and end_reason not in ("track_change", "rewind"):
            log.debug(
                "session.dropped_too_short",
                track_uri=o.track_uri,
                ms_played=ms_played,
                end_reason=end_reason,
            )
            return []

        session = PlaySession(
            track_uri=o.track_uri,
            started_at=o.started_at,
            ended_at=ended_at,
            ms_played=ms_played,
            duration_ms=o.duration_ms,
            max_progress_ms=o.max_progress_ms,
            completion_ratio=completion,
            listened_ratio=listened,
            end_reason=end_reason,
            context_uri=o.context_uri,
            context_type=o.context_type,
            device_id=o.device_id,
            device_type=o.device_type,
            device_is_private=o.device_is_private,
            shuffle=o.shuffle,
            sample_count=o.sample_count,
            seek_forward_count=o.seek_forward_count,
            first_sample_id=o.first_sample_id,
            last_sample_id=o.last_sample_id,
            joined_mid_track=o.joined_mid_track,
        )
        log.info(
            "poller.session_closed",
            track_uri=session.track_uri,
            ms_played=session.ms_played,
            completion_ratio=session.completion_ratio,
            listened_ratio=session.listened_ratio,
            end_reason=end_reason,
        )
        return [session]


def _start(s: PlayerSample) -> OpenSession:
    """새 세션을 연다.

    `started_at` 은 **관측 시각**이지 트랙의 실제 시작 시각이 아니다.
    `sampled_at - progress_ms` 로 역산하면 더 정확해 보이지만, 되감기 직후에는
    허구의 과거 시각이 나와 `UNIQUE(track_uri, started_at)` 와 시간순 정렬을
    망가뜨린다. 관측 시각은 최대 한 폴링 주기(25초)만큼 늦을 뿐이고,
    병합 윈도우(90초)가 그 오차를 흡수하도록 설계돼 있다.
    """
    assert s.track_uri is not None and s.progress_ms is not None
    return OpenSession(
        track_uri=s.track_uri,
        started_at=s.sampled_at,
        duration_ms=s.duration_ms,
        first_progress_ms=s.progress_ms,
        last_progress_ms=s.progress_ms,
        last_sample_at=s.sampled_at,
        last_progress_change_at=s.sampled_at,
        ms_played_accum=0,
        max_progress_ms=s.progress_ms,
        context_uri=s.context_uri,
        context_type=s.context_type,
        device_id=s.device_id,
        device_type=s.device_type,
        device_is_private=s.device_is_private,
        shuffle=s.shuffle_state,
        is_paused=s.is_playing is False,
        sample_count=1,
        first_sample_id=s.id,
        last_sample_id=s.id,
    )
