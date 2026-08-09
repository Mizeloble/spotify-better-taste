"""무엇/왜
세션 상태머신 테스트용 궤적 빌더. 테스트가 "샘플 딕셔너리 12개"가 아니라
"한 곡을 190초 듣고 다음 곡을 8초 만에 스킵했다"로 읽히게 한다.

설계 원칙
- 시간은 폴링 주기(기본 25초) 단위로 흐른다. 실제 폴러가 만드는 궤적과 같은
  모양이어야 테스트가 현실을 검증한다.
- 모든 메서드가 self 를 돌려줘 체이닝된다.

어떻게 확인
    uv run pytest tests/test_session_machine.py
"""

from __future__ import annotations

from spotify_taste.sessions.model import PlayerSample, SampleKind

POLL_MS = 25_000


class Trajectory:
    """폴링 샘플 목록을 선언적으로 만든다."""

    def __init__(self, start_ms: int = 1_700_000_000_000, poll_ms: int = POLL_MS) -> None:
        self._now = start_ms
        self._poll = poll_ms
        self._samples: list[PlayerSample] = []
        self._track: str | None = None
        self._duration: int | None = None
        self._progress = 0
        self._context_uri: str | None = None
        self._context_type: str | None = None
        self._device_type: str | None = "computer"
        self._shuffle: bool | None = False
        self._next_id = 1

    # --- 구성 -------------------------------------------------------------

    def context(self, uri: str | None, ctype: str | None = None) -> Trajectory:
        self._context_uri = uri
        self._context_type = ctype
        return self

    def device(self, device_type: str, *, shuffle: bool = False) -> Trajectory:
        self._device_type = device_type
        self._shuffle = shuffle
        return self

    # --- 재생 -------------------------------------------------------------

    def play(self, track_uri: str, *, duration: int, progress: int = 0) -> Trajectory:
        """새 트랙을 재생하기 시작한다 (샘플 1개 기록)."""
        self._track = track_uri
        self._duration = duration
        self._progress = progress
        self._emit(is_playing=True)
        return self

    def next(self, track_uri: str, *, duration: int, progress: int = 0) -> Trajectory:
        """다음 트랙으로 넘어간다. 한 폴링 주기 뒤에 관측된다."""
        self._now += self._poll
        self._track = track_uri
        self._duration = duration
        self._progress = progress
        self._emit(is_playing=True)
        return self

    def advance(self, seconds: float, *, ticks: int | None = None) -> Trajectory:
        """`seconds` 만큼 정상 재생한다.

        기본은 폴링 주기마다 샘플을 하나씩 만든다 — 실제 폴러와 같은 밀도.
        """
        total_ms = int(seconds * 1000)
        n = ticks if ticks is not None else max(1, round(total_ms / self._poll))
        step = total_ms // n
        for _ in range(n):
            self._now += step
            self._progress += step
            self._emit(is_playing=True)
        return self

    def pause(self, *, ticks: int = 1) -> Trajectory:
        """일시정지 상태로 `ticks` 번 관측된다 (progress 가 멈춘다)."""
        for _ in range(ticks):
            self._now += self._poll
            self._emit(is_playing=False)
        return self

    def seek_to(self, progress_ms: int) -> Trajectory:
        """재생 위치를 옮긴다 (되감기 또는 앞으로 탐색)."""
        self._now += self._poll
        self._progress = progress_ms
        self._emit(is_playing=True)
        return self

    def jitter(self, delta_ms: int) -> Trajectory:
        """벽시계와 progress 가 살짝 어긋난 샘플. 세션이 갈라지면 안 된다."""
        self._now += self._poll
        self._progress += self._poll + delta_ms
        self._emit(is_playing=True)
        return self

    def relink(self, duration: int) -> Trajectory:
        """재생 도중 duration 이 바뀐다 (track relinking)."""
        self._now += self._poll
        self._progress += self._poll
        self._duration = duration
        self._emit(is_playing=True)
        return self

    # --- 비재생 -----------------------------------------------------------

    def idle(self, *, ticks: int = 1, poll_ms: int | None = None) -> Trajectory:
        """204. 정상 상태다."""
        step = poll_ms if poll_ms is not None else self._poll
        for _ in range(ticks):
            self._now += step
            self._samples.append(PlayerSample.idle(self._now))
        return self

    def error(self, *, ticks: int = 1) -> Trajectory:
        """전송 실패. 아무것도 알 수 없는 구간."""
        for _ in range(ticks):
            self._now += self._poll
            self._samples.append(PlayerSample.error(self._now))
        return self

    def wait(self, seconds: float) -> Trajectory:
        """샘플 없이 시간만 흐른다 (프로세스 정지 구간 재현)."""
        self._now += int(seconds * 1000)
        return self

    # --- 산출 -------------------------------------------------------------

    @property
    def now(self) -> int:
        return self._now

    def samples(self) -> list[PlayerSample]:
        return list(self._samples)

    def _emit(self, *, is_playing: bool) -> None:
        assert self._track is not None
        self._samples.append(
            PlayerSample(
                sampled_at=self._now,
                kind=SampleKind.OK,
                http_status=200,
                is_playing=is_playing,
                track_uri=self._track,
                duration_ms=self._duration,
                progress_ms=max(0, self._progress),
                context_uri=self._context_uri,
                context_type=self._context_type,
                device_type=self._device_type,
                device_is_private=False,
                shuffle_state=self._shuffle,
                currently_playing_type="track",
                id=self._take_id(),
            )
        )

    def _take_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value


def run(samples: list[PlayerSample], **config_kwargs):
    """샘플을 상태머신에 흘리고 (종료된 세션들, 머신) 을 돌려준다."""
    from spotify_taste.sessions.machine import MachineConfig, SessionMachine

    machine = SessionMachine(MachineConfig(**config_kwargs) if config_kwargs else None)
    emitted = []
    for s in samples:
        emitted.extend(machine.on_sample(s))
    return emitted, machine
