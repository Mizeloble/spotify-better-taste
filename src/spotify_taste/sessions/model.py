"""무엇/왜
세션 재구성이 다루는 값들. 전부 순수 데이터이고 I/O 를 모른다.

설계 원칙
- `PlayerSample` 은 `/me/player` 응답 **한 번**을 그대로 옮긴 것이다. 해석이
  섞이지 않아야 `player_sample` 테이블에서 그대로 되살려 재계산할 수 있다.
- 샘플은 세 종류(`OK` / `IDLE` / `ERROR`)로 나뉜다. **IDLE 을 ERROR 로 다루면
  안 된다** — 204 는 "아무것도 재생 중이 아님"이라는 정상 상태이고, 오류로
  취급하면 백오프가 잘못 걸려 청취 재개를 놓친다.
- 완성도 지표가 둘인 이유는 서로 다른 질문에 답하기 때문이다.
  `completion_ratio`(위치)는 "어디까지 갔나", `listened_ratio`(시간)는
  "실제로 얼마나 들었나". 80% 지점으로 스크럽 후 끝까지 두면 전자는 1.0,
  후자는 0.2 다.

어떻게 확인
    uv run pytest tests/test_session_machine.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["OpenSession", "PlaySession", "PlayerSample", "SampleKind"]


class SampleKind(StrEnum):
    """폴링 한 번의 결과 종류."""

    #: 200 + 재생 항목 있음.
    OK = "ok"
    #: 204, 또는 200 이지만 item 이 null. **정상 상태다.**
    IDLE = "idle"
    #: 클라이언트 재시도 후에도 실패. 아무것도 알 수 없는 구간.
    ERROR = "error"


@dataclass(frozen=True)
class PlayerSample:
    """`/me/player` 응답 한 번. 해석 없음."""

    sampled_at: int  # 응답 수신 시점의 로컬 시계 (UTC epoch ms)
    kind: SampleKind = SampleKind.OK
    http_status: int = 200
    is_playing: bool | None = None
    track_uri: str | None = None
    duration_ms: int | None = None
    progress_ms: int | None = None
    context_uri: str | None = None
    context_type: str | None = None
    device_id: str | None = None
    device_type: str | None = None
    device_is_private: bool | None = None
    shuffle_state: bool | None = None
    repeat_state: str | None = None
    currently_playing_type: str | None = None
    #: API 가 준 timestamp. 기기 시계라 산술에 쓰지 않는다.
    spotify_timestamp: int | None = None
    #: `player_sample` 행 id (재구성 시 채워진다).
    id: int | None = None

    @classmethod
    def idle(cls, sampled_at: int, *, http_status: int = 204) -> PlayerSample:
        return cls(sampled_at=sampled_at, kind=SampleKind.IDLE, http_status=http_status)

    @classmethod
    def error(cls, sampled_at: int, *, http_status: int = 0) -> PlayerSample:
        return cls(sampled_at=sampled_at, kind=SampleKind.ERROR, http_status=http_status)


@dataclass
class OpenSession:
    """아직 끝나지 않은 재생. 폴러가 매 틱 갱신하고 DB 에 upsert 한다."""

    track_uri: str
    started_at: int
    duration_ms: int | None
    first_progress_ms: int

    last_progress_ms: int
    last_sample_at: int
    #: progress 가 마지막으로 움직인 시각. 일시정지 판정의 기준.
    last_progress_change_at: int

    ms_played_accum: int = 0
    max_progress_ms: int = 0

    context_uri: str | None = None
    context_type: str | None = None
    device_id: str | None = None
    device_type: str | None = None
    device_is_private: bool | None = None
    shuffle: bool | None = None

    is_paused: bool = False
    sample_count: int = 1
    seek_forward_count: int = 0
    first_sample_id: int | None = None
    last_sample_id: int | None = None

    @property
    def joined_mid_track(self) -> bool:
        """처음 봤을 때 이미 재생 중이었나.

        True 면 `ms_played` 는 하한이다 — 못 본 앞부분이 있다. 그래서
        `listened_ratio` 가 낮게 나올 수 있는데, positive 라벨이 두 지표를
        **모두** 요구하므로 오탐이 아니라 미탐 쪽으로 안전하게 기운다.
        """
        return self.first_progress_ms > 0


@dataclass(frozen=True)
class PlaySession:
    """종료된 재생 하나. `play_session` 행이 된다."""

    track_uri: str
    started_at: int
    ended_at: int
    ms_played: int
    duration_ms: int | None
    max_progress_ms: int
    completion_ratio: float | None
    listened_ratio: float | None
    end_reason: str

    context_uri: str | None = None
    context_type: str | None = None
    device_id: str | None = None
    device_type: str | None = None
    device_is_private: bool | None = None
    shuffle: bool | None = None

    sample_count: int = 0
    seek_forward_count: int = 0
    first_sample_id: int | None = None
    last_sample_id: int | None = None
    joined_mid_track: bool = False
    source: str = "poller"
    timing_confidence: str = "high"

    #: 파생 라벨 계산은 여기서 하지 않는다 — 분류/학습 계층의 몫이다.
    extra: dict = field(default_factory=dict)
