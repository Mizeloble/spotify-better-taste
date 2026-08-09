"""무엇/왜
Spotify Web API 경로 상수. 2026년 축소 이후의 **현재** 경로만 담는다.

설계 원칙
- 경로를 상수로 두는 이유는 오타 방지가 아니라 **아카이브 키 통일**이다.
  `raw_api_response.endpoint` 와 `api_schema_shape.endpoint` 가 같은 문자열을
  써야 "이 엔드포인트의 응답 모양이 언제 바뀌었나"를 조회할 수 있다. 그래서
  ID 가 들어가는 자리는 `{id}` 템플릿 그대로 저장한다.
- 사라진 엔드포인트는 지우지 않고 `GONE_*` 로 남긴다. 오래된 튜토리얼을 보고
  다시 추가하는 일을 막는 문서 역할을 한다.

어떻게 확인
    grep -rn "Endpoint[.]" src/
"""

from __future__ import annotations

from typing import Final

__all__ = ["API_BASE", "GONE", "Endpoint"]

API_BASE: Final = "https://api.spotify.com/v1"


class Endpoint:
    """살아 있는 엔드포인트. 값은 아카이브 키로도 쓰이는 템플릿이다."""

    # --- 재생 상태 (수집의 기반) ---
    PLAYER: Final = "/me/player"
    RECENTLY_PLAYED: Final = "/me/player/recently-played"

    # --- 나 ---
    ME: Final = "/me"
    MY_TRACKS: Final = "/me/tracks"
    MY_PLAYLISTS: Final = "/me/playlists"
    # {type} 자리에는 artists 또는 tracks 가 들어간다.
    # (`# type:` 으로 시작하는 주석은 mypy 가 타입 주석으로 파싱해 버린다.)
    MY_TOP: Final = "/me/top/{type}"
    # 2026-02: 저장/삭제가 /me/library 로 통합됐다.
    LIBRARY: Final = "/me/library"
    LIBRARY_CONTAINS: Final = "/me/library/contains"

    # --- 카탈로그 (전부 단건 조회. 복수 조회는 2026-02 에 사라졌다) ---
    TRACK: Final = "/tracks/{id}"
    ALBUM: Final = "/albums/{id}"
    ALBUM_TRACKS: Final = "/albums/{id}/tracks"
    ARTIST: Final = "/artists/{id}"
    ARTIST_ALBUMS: Final = "/artists/{id}/albums"

    # --- 플레이리스트 ---
    PLAYLIST: Final = "/playlists/{id}"
    # 2026-02: /tracks → /items 로 개명. 응답 키도 tracks→items, track→item.
    PLAYLIST_ITEMS: Final = "/playlists/{id}/items"
    CREATE_PLAYLIST: Final = "/me/playlists"

    # --- 검색 ---
    # 2026-02: limit 최대값 50 → 10, 기본값 20 → 5.
    SEARCH: Final = "/search"
    SEARCH_LIMIT_MAX: Final = 10


class GONE:
    """제거된 엔드포인트. 되살리려 하지 말 것 — 대체제가 없다.

    오래된 튜토리얼·블로그 예제는 대부분 이것들을 쓴다.
    """

    #: 2024-11-27 신규 앱 차단. BPM/key/energy/valence 를 얻을 방법이 없다.
    #: 이 프로젝트는 track_tag(LLM 합성 태그)로 대체한다.
    AUDIO_FEATURES: Final = "/audio-features"
    AUDIO_ANALYSIS: Final = "/audio-analysis"
    #: 추천 엔진 자체. 이 프로젝트가 LLM 으로 대체하려는 대상.
    RECOMMENDATIONS: Final = "/recommendations"
    RELATED_ARTISTS: Final = "/artists/{id}/related-artists"
    FEATURED_PLAYLISTS: Final = "/browse/featured-playlists"
    CATEGORY_PLAYLISTS: Final = "/browse/categories/{id}/playlists"
    #: 2026-02 제거.
    ARTIST_TOP_TRACKS: Final = "/artists/{id}/top-tracks"
    NEW_RELEASES: Final = "/browse/new-releases"
    MARKETS: Final = "/markets"
    #: 복수 조회 전부. N+1 을 감수하고 단건으로 가야 한다.
    BULK_TRACKS: Final = "/tracks"
    BULK_ARTISTS: Final = "/artists"
    BULK_ALBUMS: Final = "/albums"
    BULK_EPISODES: Final = "/episodes"
    BULK_SHOWS: Final = "/shows"
    #: 개명 전 경로.
    PLAYLIST_TRACKS_OLD: Final = "/playlists/{id}/tracks"


#: 제거된 필드. 응답에서 찾지 말 것.
REMOVED_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "track": ("popularity", "available_markets"),
    "album": ("popularity", "available_markets"),
    "artist": ("popularity", "followers"),
}
