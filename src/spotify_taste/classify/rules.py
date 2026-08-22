"""무엇/왜
재생의 **출처**를 판정한다. self / algorithmic / external / unknown.

이 프로젝트 전체가 이 판정 위에 서 있다. 잘못 분류하면 Spotify 의 추천으로
학습해 놓고 "내 취향 모델"이라고 부르게 되고, 평가는 자기 자신을 측정하는
순환 논증이 된다.

설계 원칙
- **순수 함수.** 증거(`Evidence`)를 받아 (origin, reason) 을 돌려줄 뿐 DB 를
  모른다. 그래야 결정표 전 행을 테스트로 고정할 수 있다.
- **null context 의 기본값은 `unknown` 이다.** 오토플레이도 null 로 오고,
  오토플레이야말로 분리하려던 알고리즘 행동이다. 양성 증거가 있을 때만
  `self` 로 승격한다. `unknown` 은 재계산 가능하므로 값싸다.
- **소유권이 이름을 이긴다.** 2·3행이 4행보다 먼저인 이유다.
- **내가 만든 추천 플레이리스트는 `algorithmic` 이다.** 알고리즘 유도가 맞고,
  그 알고리즘이 내 것일 뿐이다. 이게 없으면 Phase 4 산출물이 Phase 2 학습셋에
  재유입돼 자기강화 루프가 된다.
- 판정마다 `reason` 을 남긴다. 나중에 "왜 이렇게 분류됐지"에 답할 수 있어야
  규칙을 고칠 수 있다.

어떻게 확인
    uv run pytest tests/test_classifier.py
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .patterns import (
    DELIBERATE_START_REASONS,
    SPOTIFY_GENERATED_URI_PREFIX,
    matches_algorithmic_name,
)

__all__ = ["ContextSource", "Evidence", "Origin", "classify"]

#: Liked Songs 재생의 컨텍스트. 계정마다 uri 가 다르므로 타입으로 판정한다.
COLLECTION_TYPES = frozenset({"collection", "collectionyourepisodes"})
#: 이보다 오래 조용하다가 시작했으면 사람이 직접 튼 것으로 본다.
MANUAL_START_SILENCE_MS = 60_000


class ContextSource(StrEnum):
    """컨텍스트를 어디서 얻었나. 관측과 유추를 섞지 않기 위한 표식이다."""

    OBSERVED = "observed"  # /me/player 가 실제로 준 값
    SHUFFLE_SEQUENCE = "shuffle_sequence"  # 기술 로그에서 유추 (정확도 84.5%)


class Origin(StrEnum):
    SELF = "self"
    ALGORITHMIC = "algorithmic"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    """판정에 쓰이는 모든 입력. DB 조회 결과를 여기 담아 넘긴다."""

    context_uri: str | None = None
    context_type: str | None = None

    # 플레이리스트 컨텍스트일 때만 채워진다.
    playlist_owner_id: str | None = None
    playlist_name: str | None = None
    playlist_is_generated: bool = False
    playlist_resolved: bool = False

    me_id: str | None = None

    # null-context 승격에 쓰이는 양성 증거.
    track_in_library: bool = False
    track_in_own_playlist: bool = False
    silence_before_ms: int | None = None
    shuffle: bool | None = None

    #: 재생이 어떻게 시작됐나 (export 전용: clickrow/trackdone/fwdbtn/…).
    reason_start: str | None = None

    #: 컨텍스트의 출처. 유추한 컨텍스트는 해석에 실패했을 때 직접 증거를
    #: 지우지 못한다 (아래 결정표 참조).
    context_source: ContextSource = ContextSource.OBSERVED

    #: context 가 항상 null 로 오는 계정을 위한 축소 규칙셋.
    degraded_mode: bool = False


def classify(ev: Evidence) -> tuple[Origin, str]:
    """출처와 그 근거를 돌려준다. 위에서부터 첫 일치.

    유추한 컨텍스트로 내린 판정에는 `inferred:` 접두사가 붙는다 — 나중에
    "이 판정이 관측이었나 추측이었나"를 reason 만 보고 가를 수 있어야 한다.
    """
    origin, reason = _decide(ev)
    if ev.context_source != ContextSource.OBSERVED and not reason.startswith("null_ctx"):
        reason = f"inferred:{reason}"
    return origin, reason


def _decide(ev: Evidence) -> tuple[Origin, str]:
    ctype = (ev.context_type or "").lower()

    # --- 플레이리스트 컨텍스트 ---
    if ctype == "playlist":
        if ev.playlist_is_generated:
            # (1) 내가 만든 추천 플레이리스트. 소유권보다 먼저 본다.
            return Origin.ALGORITHMIC, "own_generated_playlist"
        if ev.playlist_owner_id and ev.me_id and ev.playlist_owner_id == ev.me_id:
            # (2) 이름 패턴이 소유권을 이길 수 없다.
            return Origin.SELF, "playlist_owner_me"
        if ev.playlist_owner_id == "spotify":
            return Origin.ALGORITHMIC, "playlist_owner_spotify"
        matched = matches_algorithmic_name(ev.playlist_name)
        if matched:
            return Origin.ALGORITHMIC, f"playlist_name:{matched}"
        if ev.playlist_resolved and ev.playlist_owner_id:
            return Origin.EXTERNAL, "playlist_owner_other"
        # 2024-11 이후 Spotify 생성 플레이리스트는 /playlists/{id} 가 404 라
        # owner 해석이 영원히 실패한다. ID 접두사가 남은 유일한 신호이고,
        # 확인된 소유권 증거(위의 규칙들)보다는 뒤에 둔다.
        if ev.context_uri and ev.context_uri.startswith(SPOTIFY_GENERATED_URI_PREFIX):
            return Origin.ALGORITHMIC, "spotify_generated_uri_prefix"
        if ev.context_source != ContextSource.OBSERVED:
            # 유추한 컨텍스트인데 해석까지 실패했다. 여기서 unknown 을 돌려주면
            # 라이브러리 보유 같은 **직접 증거로 이미 self 였던 행이 퇴행한다**
            # (정규화 없이 돌렸을 때 349건). 추측이 관측을 지울 수는 없다.
            return _direct_evidence(ev)
        # owner 를 못 알아냈다. 추측하지 않는다 — 캐시가 채워지면 자동 치유된다.
        return Origin.UNKNOWN, "playlist_owner_unresolved"

    # --- 라디오 ---
    if ev.context_uri and ":radio:" in ev.context_uri:
        return Origin.ALGORITHMIC, "radio_context"

    # --- 앨범/아티스트/좋아요 ---
    if ctype == "album":
        return Origin.SELF, "album_context"
    if ctype == "artist":
        # 아티스트 페이지 재생은 Spotify 가 정한 순서라 준-알고리즘적이라는
        # 이견이 가능하다. 다음 rules 버전에서 재검토하라고 이름에 남긴다.
        return Origin.SELF, "artist_context_assumed"
    if ctype in COLLECTION_TYPES:
        return Origin.SELF, "liked_songs_context"
    if ctype == "single_track":
        # 컨텍스트가 곡 자신이다 — 목록 없이 그 곡 하나를 튼 재생.
        return Origin.SELF, "single_track_context"

    # --- context 없음 ---
    if not ev.context_uri and not ctype:
        return _direct_evidence(ev)

    return Origin.UNKNOWN, f"unhandled_context_type:{ctype or 'none'}"


def _direct_evidence(ev: Evidence) -> tuple[Origin, str]:
    """컨텍스트를 빼고 남은 직접 증거만으로 판정한다.

    context 가 없을 때의 기본 경로이자, 유추한 컨텍스트를 해석하지 못했을 때의
    되돌아갈 자리다.
    """
    if ev.track_in_library:
        return Origin.SELF, "null_ctx_library_hit"
    if ev.track_in_own_playlist:
        return Origin.SELF, "null_ctx_own_playlist"
    if ev.reason_start in DELIBERATE_START_REASONS:
        # Spotify 가 직접 기록한 자발적 행동 — 그 곡을 클릭했거나 되감아
        # 다시 들었다. 계정의 context 품질과 무관하게 유효한 증거라
        # 성능저하 모드 검사보다 앞에 둔다.
        return Origin.SELF, f"null_ctx_deliberate:{ev.reason_start}"
    if ev.degraded_mode:
        # 대비가 없는 계정에서는 무음 휴리스틱이 무의미하다.
        return Origin.UNKNOWN, "null_ctx_degraded_no_evidence"
    if (
        ev.silence_before_ms is not None
        and ev.silence_before_ms >= MANUAL_START_SILENCE_MS
        and ev.shuffle is False
    ):
        return Origin.SELF, "null_ctx_manual_start"
    # 오토플레이가 여기 온다. self 로 넣으면 Spotify 의 추천으로 학습하게 된다.
    return Origin.UNKNOWN, "null_ctx_no_evidence"
