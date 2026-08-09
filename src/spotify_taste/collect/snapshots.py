"""무엇/왜
주기적 스냅샷. 라이브러리·플레이리스트·`/me/top` 을 야간에 훑는다.

설계 원칙
- **`/me/top` 은 현재 상태만 준다.** 스냅샷을 찍지 않은 주는 영구 소실이고
  소급 수집이 불가능하다. Phase 5 의 취향 드리프트 베이스라인이 여기 걸려 있어
  1일차부터 찍는다.
- 라이브러리에서 사라진 트랙도 **행을 지우지 않고** `removed_at` 만 채운다.
  "언제 뺐는가"는 그 자체로 선호 신호다.
- 플레이리스트 항목은 `snapshot_id` 별로 쌓는다. Daily Mix 처럼 내용이 매일
  바뀌는 것도 이력이 남는다.

어떻게 확인
    uv run spotify-taste snapshot library
    uv run pytest tests/test_snapshots.py
"""

from __future__ import annotations

import sqlite3

import structlog

from .._common import epoch_ms_to_kst, iso_to_epoch_ms, now_ms
from ..db.connection import tx
from ..spotify.client import SpotifyApiError, SpotifyClient
from ..spotify.endpoints import Endpoint

__all__ = ["snapshot_library", "snapshot_me", "snapshot_playlists", "snapshot_top"]

log = structlog.get_logger(__name__)

TIME_RANGES = ("short_term", "medium_term", "long_term")
TOP_LIMIT = 50


async def snapshot_me(conn: sqlite3.Connection, client: SpotifyClient) -> dict | None:
    """`GET /me`. 분류기가 `owner.id == me` 를 판정하려면 반드시 필요하다."""
    resp = await client.get(Endpoint.ME, role="snapshot", source_job="snapshot")
    if resp is None or not isinstance(resp.json, dict):
        return None
    me = resp.json
    with tx(conn):
        conn.execute(
            "INSERT INTO me (id, display_name, country, product, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET display_name = excluded.display_name, "
            "country = excluded.country, product = excluded.product, "
            "fetched_at = excluded.fetched_at",
            (
                me.get("id"),
                me.get("display_name"),
                me.get("country"),
                me.get("product"),
                now_ms(),
            ),
        )
    log.info("snapshot.me", product=me.get("product"))
    return me


async def snapshot_library(conn: sqlite3.Connection, client: SpotifyClient) -> int:
    """`GET /me/tracks`. 사라진 트랙은 지우지 않고 removed_at 을 채운다."""
    items = await client.paginate(
        Endpoint.MY_TRACKS, limit=50, role="snapshot", source_job="snapshot"
    )
    observed = now_ms()
    seen: set[str] = set()

    with tx(conn):
        for entry in items:
            track = entry.get("track")
            if not isinstance(track, dict) or not track.get("uri"):
                continue
            uri = str(track["uri"])
            seen.add(uri)
            added_at = entry.get("added_at")
            conn.execute(
                "INSERT INTO library_track (track_uri, added_at, observed_at, removed_at) "
                "VALUES (?, ?, ?, NULL) "
                "ON CONFLICT (track_uri) DO UPDATE SET observed_at = excluded.observed_at, "
                "removed_at = NULL",
                (uri, _iso_or_none(added_at), observed),
            )
            _ensure_track_row(conn, uri)

        # 이번에 안 보인 것은 라이브러리에서 뺀 것이다. 행은 남긴다.
        if seen:
            conn.execute(
                "UPDATE library_track SET removed_at = ? "
                "WHERE removed_at IS NULL AND observed_at < ?",
                (observed, observed),
            )
    log.info("snapshot.library", tracks=len(seen))
    return len(seen)


async def snapshot_playlists(conn: sqlite3.Connection, client: SpotifyClient) -> int:
    """`GET /me/playlists` + 각 플레이리스트의 항목.

    2026-02 에 `/playlists/{id}/tracks` 가 `/items` 로 개명됐고 응답 키도
    `tracks`→`items`, `track`→`item` 으로 바뀌었다.
    """
    playlists = await client.paginate(
        Endpoint.MY_PLAYLISTS, limit=50, role="snapshot", source_job="snapshot"
    )
    now = now_ms()
    count = 0

    for pl in playlists:
        if not pl.get("uri"):
            continue
        owner = _as_dict(pl.get("owner"))
        tracks = _as_dict(pl.get("tracks"))
        with tx(conn):
            conn.execute(
                "INSERT INTO playlist_meta (uri, id, name, description, owner_id, "
                "owner_display_name, owner_uri, is_collaborative, is_public, snapshot_id, "
                "total_items, fetched_at, fetch_status, http_status, refresh_after) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', 200, ?) "
                "ON CONFLICT (uri) DO UPDATE SET name = excluded.name, "
                "owner_id = excluded.owner_id, snapshot_id = excluded.snapshot_id, "
                "total_items = excluded.total_items, fetched_at = excluded.fetched_at, "
                "fetch_status = 'ok', refresh_after = excluded.refresh_after",
                (
                    pl["uri"],
                    pl.get("id"),
                    pl.get("name"),
                    pl.get("description"),
                    owner.get("id"),
                    owner.get("display_name"),
                    owner.get("uri"),
                    int(bool(pl.get("collaborative"))),
                    int(bool(pl.get("public"))),
                    pl.get("snapshot_id"),
                    tracks.get("total"),
                    now,
                    now + 30 * 86_400_000,
                ),
            )
        count += 1
    log.info("snapshot.playlists", playlists=count)
    return count


async def snapshot_playlist_items(
    conn: sqlite3.Connection, client: SpotifyClient, playlist_uri: str, playlist_id: str
) -> int:
    """한 플레이리스트의 수록곡. 개명된 `/items` 경로를 쓴다."""
    try:
        items = await client.paginate(
            Endpoint.PLAYLIST_ITEMS,
            path_params={"id": playlist_id},
            limit=50,
            role="snapshot",
            source_job="snapshot",
        )
    except SpotifyApiError as exc:
        log.warning("snapshot.playlist_items_failed", playlist=playlist_uri, status=exc.status)
        return 0

    observed = now_ms()
    snapshot_id = str(observed)
    stored = 0
    with tx(conn):
        for position, entry in enumerate(items):
            # 개명 이후 키는 `item`. 구 응답 호환으로 `track` 도 본다.
            payload = entry.get("item") or entry.get("track")
            if not isinstance(payload, dict) or not payload.get("uri"):
                continue
            conn.execute(
                "INSERT INTO playlist_item (playlist_uri, track_uri, position, added_at, "
                "added_by, snapshot_id, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (playlist_uri, snapshot_id, track_uri) DO NOTHING",
                (
                    playlist_uri,
                    payload["uri"],
                    position,
                    _iso_or_none(entry.get("added_at")),
                    _as_dict(entry.get("added_by")).get("id"),
                    snapshot_id,
                    observed,
                ),
            )
            _ensure_track_row(conn, str(payload["uri"]))
            stored += 1
    return stored


async def snapshot_top(conn: sqlite3.Connection, client: SpotifyClient) -> int:
    """`GET /me/top/{artists,tracks}` 세 구간 전부.

    찍지 않은 주는 영구 소실이므로 실패해도 나머지는 계속 시도한다.
    """
    captured_on = f"{epoch_ms_to_kst(now_ms()):%Y-%m-%d}"
    stored = 0
    for item_type in ("artists", "tracks"):
        for time_range in TIME_RANGES:
            try:
                resp = await client.get(
                    Endpoint.MY_TOP,
                    path_params={"type": item_type},
                    params={"time_range": time_range, "limit": TOP_LIMIT},
                    role="snapshot",
                    source_job="snapshot",
                )
            except SpotifyApiError as exc:
                log.warning(
                    "snapshot.top_failed", type=item_type, range=time_range, status=exc.status
                )
                continue
            if resp is None or not isinstance(resp.json, dict):
                continue
            singular = "artist" if item_type == "artists" else "track"
            with tx(conn):
                for rank, item in enumerate(resp.json.get("items") or []):
                    if not isinstance(item, dict) or not item.get("uri"):
                        continue
                    conn.execute(
                        "INSERT INTO top_item (item_type, time_range, captured_on, rank, uri) "
                        "VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT (item_type, time_range, captured_on, rank) "
                        "DO UPDATE SET uri = excluded.uri",
                        (singular, time_range, captured_on, rank, item["uri"]),
                    )
                    stored += 1
    log.info("snapshot.top", rows=stored, captured_on=captured_on)
    return stored


def _ensure_track_row(conn: sqlite3.Connection, uri: str) -> None:
    """카탈로그 백필 큐에 트랙을 올려 둔다 (아직 메타는 없다)."""
    conn.execute(
        "INSERT INTO track (uri, fetch_status) VALUES (?, 'pending') ON CONFLICT (uri) DO NOTHING",
        (uri,),
    )


def _as_dict(value: object) -> dict:
    """중첩 객체를 안전하게 꺼낸다 — Spotify 는 통째로 null 을 주는 일이 흔하다."""
    return value if isinstance(value, dict) else {}


def _iso_or_none(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return iso_to_epoch_ms(value)
    except ValueError:
        return None
