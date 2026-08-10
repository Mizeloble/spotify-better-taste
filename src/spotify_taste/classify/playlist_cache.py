"""무엇/왜
플레이리스트 소유자 캐시. 분류기가 "이 플레이리스트를 누가 만들었나"를
알아야 self / algorithmic / external 을 가를 수 있다.

설계 원칙
- **실패도 캐시한다.** 삭제된 플레이리스트를 매 분류마다 영원히 재조회하는
  것을 막는다. `fetch_status != 'ok'` 이면서 `refresh_after` 가 지난 것만
  다시 시도하므로 `unknown` 행이 시간이 지나면 자동으로 치유된다.
- TTL 이 다른 이유: 이름과 소유자는 안정적이라 성공은 30일을 간다.
  Daily Mix 는 **내용**이 매일 바뀌지만 소유자는 그대로다. 404/403 은 7일,
  일시적 오류는 6시간.
- 한 번에 조회하는 개수를 제한한다. 복수 조회 엔드포인트가 사라져 1엔티티=1콜이라,
  분류가 갑자기 수백 콜을 쏘면 폴러가 굶는다.

어떻게 확인
    uv run pytest tests/test_playlist_cache.py
"""

from __future__ import annotations

import sqlite3

import structlog

from .._common import now_ms
from ..db.connection import tx
from ..db.queries import SELECT_PLAYLISTS_NEEDING_REFRESH, UPSERT_PLAYLIST_STATUS
from ..spotify.client import SpotifyApiError, SpotifyClient
from ..spotify.endpoints import Endpoint

__all__ = ["TTL_ERROR_MS", "TTL_MISSING_MS", "TTL_OK_MS", "refresh_playlist_cache"]

log = structlog.get_logger(__name__)

DAY_MS = 86_400_000
#: 이름과 소유자는 안정적이다 (Daily Mix 도 내용만 바뀐다).
TTL_OK_MS = 30 * DAY_MS
#: 삭제됐거나 접근 불가. 되살아날 수도 있으니 영구 포기는 하지 않는다.
TTL_MISSING_MS = 7 * DAY_MS
#: 일시적 오류는 곧 다시 본다.
TTL_ERROR_MS = 6 * 3_600_000

#: 한 번의 분류에서 새로 조회할 최대 개수.
DEFAULT_MAX_LOOKUPS = 50


async def refresh_playlist_cache(
    conn: sqlite3.Connection,
    client: SpotifyClient,
    *,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
) -> int:
    """세션이 참조하는데 아직 모르는 플레이리스트의 소유자를 채운다."""
    now = now_ms()
    rows = conn.execute(SELECT_PLAYLISTS_NEEDING_REFRESH, (now, max_lookups)).fetchall()
    resolved = 0

    for row in rows:
        uri = str(row["uri"])
        playlist_id = _id_from_uri(uri)
        if playlist_id is None:
            _store(conn, uri, None, status="not_found", http_status=None, ttl=TTL_MISSING_MS)
            continue

        try:
            resp = await client.get(
                Endpoint.PLAYLIST,
                path_params={"id": playlist_id},
                role="snapshot",
                source_job="classify",
            )
        except SpotifyApiError as exc:
            # 404/403 은 오래, 그 밖의 오류는 짧게 캐시한다.
            status = (
                "not_found" if exc.status == 404 else "forbidden" if exc.status == 403 else "error"
            )
            ttl = TTL_MISSING_MS if exc.status in (403, 404) else TTL_ERROR_MS
            _store(conn, uri, None, status=status, http_status=exc.status, ttl=ttl)
            continue

        if resp is None:
            # 레이트리밋으로 슬롯을 못 얻었다. 다음 실행에 다시 본다.
            break
        if not isinstance(resp.json, dict):
            _store(conn, uri, None, status="error", http_status=resp.status, ttl=TTL_ERROR_MS)
            continue

        _store(conn, uri, resp.json, status="ok", http_status=resp.status, ttl=TTL_OK_MS)
        resolved += 1

    if rows:
        log.info("classify.playlist_cache_refreshed", requested=len(rows), resolved=resolved)
    return resolved


def _store(
    conn: sqlite3.Connection,
    uri: str,
    body: dict | None,
    *,
    status: str,
    http_status: int | None,
    ttl: int,
) -> None:
    now = now_ms()
    payload = body if isinstance(body, dict) else {}
    owner = _as_dict(payload.get("owner"))
    tracks = _as_dict(payload.get("tracks"))
    with tx(conn):
        conn.execute(
            UPSERT_PLAYLIST_STATUS,
            (
                uri,
                payload.get("id") or _id_from_uri(uri),
                payload.get("name"),
                owner.get("id"),
                owner.get("display_name"),
                owner.get("uri"),
                payload.get("snapshot_id"),
                tracks.get("total"),
                now,
                status,
                http_status,
                now + ttl,
            ),
        )


def _as_dict(value: object) -> dict:
    """중첩 객체를 안전하게 꺼낸다 — Spotify 는 통째로 null 을 주는 일이 흔하다."""
    return value if isinstance(value, dict) else {}


def _id_from_uri(uri: str) -> str | None:
    """`spotify:playlist:37i9...` → `37i9...`."""
    parts = uri.split(":")
    return parts[2] if len(parts) >= 3 and parts[1] == "playlist" and parts[2] else None
