"""무엇/왜
API 응답을 우리 값 객체로 옮기는 파서. **해석하지 않고 옮기기만 한다.**

설계 원칙
- 모든 필드를 `.get()` 으로 읽는다. Spotify 가 필드를 계속 지우고 있어
  `KeyError` 로 수집기가 죽는 상황을 만들면 안 된다. 없으면 None 이고,
  그 None 자체가 나중에 분석할 데이터다.
- 판정은 하지 않는다. "context 가 null 이면 self" 같은 규칙은 `classify/` 의
  몫이다. 여기서 하면 규칙이 바뀌었을 때 재계산할 수 없다.
- `sampled_at` 은 **로컬 수신 시각**이다. 응답의 `timestamp` 는 기기에서 재생
  상태가 마지막으로 바뀐 시각이고 기기 시계는 흐르므로, 산술에 쓰지 않고
  드리프트 연구용으로만 병행 저장한다.

어떻게 확인
    uv run pytest tests/test_schemas.py
"""

from __future__ import annotations

from typing import Any

from .._common import iso_to_epoch_ms
from ..sessions.model import PlayerSample, SampleKind

__all__ = ["RecentPlay", "parse_player_sample", "parse_recently_played"]


def parse_player_sample(body: Any, *, sampled_at: int, http_status: int = 200) -> PlayerSample:
    """`GET /me/player` 응답 하나를 `PlayerSample` 로.

    204 이거나 재생 항목이 없으면 IDLE 로 표시한다 — **오류가 아니다.**
    """
    if http_status == 204 or not isinstance(body, dict) or not body:
        return PlayerSample.idle(sampled_at, http_status=http_status)

    item = body.get("item")
    if not isinstance(item, dict):
        # 광고 재생 중이거나 알 수 없는 타입. 트랙 정보가 없으니 유휴로 다룬다.
        return PlayerSample(
            sampled_at=sampled_at,
            kind=SampleKind.IDLE,
            http_status=http_status,
            is_playing=_as_bool(body.get("is_playing")),
            currently_playing_type=_as_str(body.get("currently_playing_type")),
        )

    device = _as_dict(body.get("device"))
    context = _as_dict(body.get("context"))

    return PlayerSample(
        sampled_at=sampled_at,
        kind=SampleKind.OK,
        http_status=http_status,
        is_playing=_as_bool(body.get("is_playing")),
        track_uri=_as_str(item.get("uri")),
        duration_ms=_as_int(item.get("duration_ms")),
        progress_ms=_as_int(body.get("progress_ms")) or 0,
        # context 는 계정에 따라 항상 null 로 오기도 하고, Liked Songs 재생 시
        # 정상적으로도 null 이다. 그 구분은 classify/ 가 한다.
        context_uri=_as_str(context.get("uri")),
        context_type=_as_str(context.get("type")),
        device_id=_as_str(device.get("id")),
        device_type=_as_str(device.get("type")),
        device_is_private=_as_bool(device.get("is_private_session")),
        shuffle_state=_as_bool(body.get("shuffle_state")),
        repeat_state=_as_str(body.get("repeat_state")),
        currently_playing_type=_as_str(body.get("currently_playing_type")),
        spotify_timestamp=_as_int(body.get("timestamp")),
    )


class RecentPlay:
    """`recently-played` 항목 하나. 폴러 샘플과 **다른 타입**인 것이 중요하다.

    여기에는 `ms_played` 가 없다. 같은 타입으로 만들면 언젠가 실측과 추정이
    같은 컬럼에 섞인다.
    """

    __slots__ = ("context_type", "context_uri", "duration_ms", "played_at", "track_uri")

    def __init__(
        self,
        *,
        track_uri: str,
        played_at: int,
        duration_ms: int | None,
        context_uri: str | None,
        context_type: str | None,
    ) -> None:
        self.track_uri = track_uri
        self.played_at = played_at
        self.duration_ms = duration_ms
        self.context_uri = context_uri
        self.context_type = context_type

    def __repr__(self) -> str:
        return f"RecentPlay({self.track_uri}, played_at={self.played_at})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RecentPlay):
            return NotImplemented
        return (self.track_uri, self.played_at) == (other.track_uri, other.played_at)


def parse_recently_played(body: Any) -> list[RecentPlay]:
    """`GET /me/player/recently-played` 응답을 항목 목록으로.

    played_at 이 없거나 트랙 정보가 없는 항목은 조용히 건너뛴다 — 재생하지
    않은 곡이 중복 항목으로 들어오는 사례가 보고돼 있다.
    """
    if not isinstance(body, dict):
        return []
    out: list[RecentPlay] = []
    for entry in body.get("items") or []:
        if not isinstance(entry, dict):
            continue
        track = entry.get("track")
        if not isinstance(track, dict):
            continue
        uri = _as_str(track.get("uri"))
        played_at_raw = _as_str(entry.get("played_at"))
        if not uri or not played_at_raw:
            continue
        try:
            played_at = iso_to_epoch_ms(played_at_raw)
        except ValueError:
            continue
        context = _as_dict(entry.get("context"))
        out.append(
            RecentPlay(
                track_uri=uri,
                played_at=played_at,
                duration_ms=_as_int(track.get("duration_ms")),
                context_uri=_as_str(context.get("uri")),
                context_type=_as_str(context.get("type")),
            )
        )
    return out


def _as_dict(value: Any) -> dict[str, Any]:
    """중첩 객체를 안전하게 꺼낸다. null 이면 빈 dict — Spotify 는 `context`,
    `device` 를 통째로 null 로 주는 일이 흔하다."""
    return value if isinstance(value, dict) else {}


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
