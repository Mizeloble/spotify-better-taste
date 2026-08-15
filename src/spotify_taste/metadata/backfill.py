"""무엇/왜
카탈로그 메타데이터를 채운다. 2026-02 에 복수 조회 엔드포인트가 전부 사라져
**1엔티티 = 1콜**이 됐고, 그래서 이건 "가끔 도는 배치"가 아니라 예산제로
관리해야 하는 상시 작업이다.

규모 감각: 라이브러리 5,000곡 + 스트리밍 히스토리의 고유 트랙 20,000 +
아티스트/앨범 6,000 ≈ **30,000콜**. 2 rps 상한이면 연속 4.2시간이라
한 번에 때릴 수 없다.

설계 원칙
- **재개 가능하고 예산제다.** `--max-calls` 를 정확히 지키고, 남은 것은
  다음 밤에 이어서 한다. 상태가 DB(`fetch_status`)에 있으므로 중간에 죽어도
  잃는 것이 없다.
- **재생 횟수 내림차순.** 실제로 듣는 트랙이 첫날 밤에 해결되고 롱테일은
  천천히 빠진다. 순서가 곧 체감 속도다.
- **404 는 다시 시도하지 않는다.** 없는 트랙을 매일 밤 다시 물어보는 것은
  예산 낭비다. 일시적 오류(`error`)만 다음 회차에 재시도한다.
- **폴러의 활성 시간대를 피한다.** 야간에 0.5초 간격으로 조용히 흐른다.

어떻게 확인
    uv run spotify-taste backfill run --max-calls 5
    uv run spotify-taste backfill status
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import structlog

from .._common import now_ms
from ..db.connection import tx
from ..db.queries import (
    ENQUEUE_TRACKS_FROM_SESSIONS,
    ENSURE_ALBUM,
    ENSURE_ARTIST,
    LINK_TRACK_ARTIST,
    MARK_FETCH_FAILED,
    SELECT_PENDING_ALBUMS,
    SELECT_PENDING_ARTISTS,
    SELECT_PENDING_TRACKS,
    UPDATE_ALBUM_META,
    UPDATE_ARTIST_META,
    UPDATE_TRACK_META,
)
from ..spotify.client import SpotifyApiError, SpotifyClient
from ..spotify.endpoints import Endpoint
from ..spotify.ratelimit import RetryAfterTooLong

__all__ = [
    "BackfillResult",
    "QueueProjection",
    "enqueue_from_sessions",
    "project_queue",
    "run_backfill",
]

log = structlog.get_logger(__name__)


@dataclass
class BackfillResult:
    calls: int = 0
    tracks: int = 0
    artists: int = 0
    albums: int = 0
    not_found: int = 0
    errors: int = 0
    stopped_reason: str = "budget"

    @property
    def resolved(self) -> int:
        return self.tracks + self.artists + self.albums


@dataclass
class QueueProjection:
    """남은 일감과 완료 시점 추정. 순수 계산이라 DB 없이 검증한다."""

    pending: int
    #: 트랙을 풀면 큐에 새로 올라올 아티스트·앨범 수 (상한).
    growth: int
    #: 최근 실측 처리량(건/밤). 실측이 없으면 예산값으로 떨어진다.
    per_night: float
    measured: bool

    @property
    def total(self) -> int:
        return self.pending + self.growth

    @property
    def nights(self) -> int:
        if self.per_night <= 0:
            return 0
        return -(-self.total // int(self.per_night))  # 올림


def project_queue(
    status: dict,
    *,
    unqueued: int,
    resolved_recent: int,
    nights_recent: int,
    budget_per_night: int,
) -> QueueProjection:
    """큐 깊이와 실측 처리량으로 완료 시점을 잡는다.

    예산(`--max-calls`)으로 나누면 안 된다. dev 모드 쿼터가 하루 600콜쯤에서
    끊어 `backfill.aborted_rate_limit` 로 물러나는 것이 정상이라, 예산 기준
    추정은 몇 배씩 낙관적이다 (2026-08-15 실측: 예산 3,000 / 실제 600).

    트랙을 풀면 그 응답에서 아티스트·앨범이 새로 큐에 오르므로 남은 트랙만큼
    큐가 더 자란다. 반복 아티스트는 한 번만 세지므로 이 증가분은 **상한**이다.
    """
    pending_tracks = int(status["tracks_pending"]) + unqueued
    pending = pending_tracks + int(status["artists_pending"]) + int(status["albums_pending"])

    tracks_ok = int(status["tracks_ok"])
    if tracks_ok > 0:
        # 지금까지 트랙 하나를 풀 때마다 아티스트·앨범이 몇 개 딸려 왔는지.
        discovered = (
            int(status["artists_ok"])
            + int(status["artists_pending"])
            + int(status["albums_ok"])
            + int(status["albums_pending"])
        )
        growth = int(pending_tracks * discovered / tracks_ok)
    else:
        growth = 0

    measured = nights_recent > 0 and resolved_recent > 0
    per_night = resolved_recent / nights_recent if measured else float(budget_per_night)
    return QueueProjection(pending=pending, growth=growth, per_night=per_night, measured=measured)


def enqueue_from_sessions(conn: sqlite3.Connection) -> None:
    """세션에서 본 트랙을 큐에 올린다 (아직 메타는 없다)."""
    with tx(conn):
        conn.execute(ENQUEUE_TRACKS_FROM_SESSIONS)


async def run_backfill(
    conn: sqlite3.Connection,
    client: SpotifyClient,
    *,
    max_calls: int = 3000,
) -> BackfillResult:
    """예산 안에서 카탈로그를 채운다.

    트랙을 먼저 해결하는 이유: 트랙 응답이 아티스트/앨범 URI 를 알려 주므로
    그것들이 다시 큐가 된다.
    """
    enqueue_from_sessions(conn)
    result = BackfillResult()

    for kind, query, handler in (
        ("track", SELECT_PENDING_TRACKS, _fetch_track),
        ("artist", SELECT_PENDING_ARTISTS, _fetch_artist),
        ("album", SELECT_PENDING_ALBUMS, _fetch_album),
    ):
        remaining = max_calls - result.calls
        if remaining <= 0:
            break
        rows = conn.execute(query, (remaining,)).fetchall()
        for row in rows:
            if result.calls >= max_calls:
                break
            try:
                await handler(conn, client, str(row["uri"]), result)
            except RetryAfterTooLong:
                # 수집기 안에서 한 시간 자느니 배치를 접고 다음 실행에 맡긴다.
                result.stopped_reason = "rate_limit"
                log.warning("backfill.aborted_rate_limit", calls=result.calls)
                return result
            result.calls += 1
        log.info("backfill.kind_done", kind=kind, calls=result.calls)

    log.info(
        "backfill.done",
        calls=result.calls,
        resolved=result.resolved,
        not_found=result.not_found,
    )
    return result


async def _fetch_track(
    conn: sqlite3.Connection, client: SpotifyClient, uri: str, result: BackfillResult
) -> bool:
    body = await _get(conn, client, Endpoint.TRACK, uri, "track", result)
    if body is None:
        return False
    album = _as_dict(body.get("album"))
    external = _as_dict(body.get("external_ids"))

    with tx(conn):
        conn.execute(
            UPDATE_TRACK_META,
            (
                body.get("id"),
                body.get("name"),
                album.get("uri"),
                body.get("duration_ms"),
                int(bool(body.get("explicit"))),
                external.get("isrc"),
                body.get("disc_number"),
                body.get("track_number"),
                int(bool(body.get("is_local"))),
                now_ms(),
                200,
                None,
                uri,
            ),
        )
        # 트랙 응답이 아티스트·앨범을 알려 준다 → 그것들이 다시 큐가 된다.
        if album.get("uri"):
            conn.execute(ENSURE_ALBUM, (album["uri"],))
        for position, artist in enumerate(body.get("artists") or []):
            if not isinstance(artist, dict) or not artist.get("uri"):
                continue
            conn.execute(ENSURE_ARTIST, (artist["uri"],))
            conn.execute(LINK_TRACK_ARTIST, (uri, artist["uri"], position))
    result.tracks += 1
    return True


async def _fetch_artist(
    conn: sqlite3.Connection, client: SpotifyClient, uri: str, result: BackfillResult
) -> bool:
    body = await _get(conn, client, Endpoint.ARTIST, uri, "artist", result)
    if body is None:
        return False
    images = body.get("images") if isinstance(body.get("images"), list) else []
    first_image = images[0] if images and isinstance(images[0], dict) else {}
    with tx(conn):
        conn.execute(
            UPDATE_ARTIST_META,
            (
                body.get("id"),
                body.get("name"),
                # audio-features 가 죽은 뒤 남은 취향 벡터의 주 재료.
                json.dumps(body.get("genres") or [], ensure_ascii=False),
                first_image.get("url"),
                now_ms(),
                200,
                None,
                uri,
            ),
        )
    result.artists += 1
    return True


async def _fetch_album(
    conn: sqlite3.Connection, client: SpotifyClient, uri: str, result: BackfillResult
) -> bool:
    body = await _get(conn, client, Endpoint.ALBUM, uri, "album", result)
    if body is None:
        return False
    with tx(conn):
        conn.execute(
            UPDATE_ALBUM_META,
            (
                body.get("id"),
                body.get("name"),
                body.get("album_type"),
                # popularity 가 제거된 지금 era 를 알 수 있는 유일한 신호.
                body.get("release_date"),
                body.get("release_date_precision"),
                body.get("label"),
                body.get("total_tracks"),
                json.dumps(body.get("genres") or [], ensure_ascii=False),
                now_ms(),
                200,
                None,
                uri,
            ),
        )
    result.albums += 1
    return True


def _as_dict(value: object) -> dict:
    """중첩 객체를 안전하게 꺼낸다 — Spotify 는 통째로 null 을 주는 일이 흔하다."""
    return value if isinstance(value, dict) else {}


async def _get(
    conn: sqlite3.Connection,
    client: SpotifyClient,
    endpoint: str,
    uri: str,
    table: str,
    result: BackfillResult,
) -> dict | None:
    """단건 조회. 실패는 `fetch_status` 에 못 박는다."""
    entity_id = uri.rsplit(":", 1)[-1]
    try:
        resp = await client.get(
            endpoint,
            path_params={"id": entity_id},
            role="backfill",
            source_job="backfill",
        )
    except SpotifyApiError as exc:
        # 404 는 영구 표시 — 없는 것을 매일 밤 다시 묻지 않는다.
        status = "not_found" if exc.status in (404, 400) else "error"
        with tx(conn):
            conn.execute(MARK_FETCH_FAILED.format(table=table), (status, exc.status, now_ms(), uri))
        if status == "not_found":
            result.not_found += 1
        else:
            result.errors += 1
        return None

    if resp is None or not isinstance(resp.json, dict):
        return None
    return resp.json
