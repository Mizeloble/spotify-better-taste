"""메타데이터 백필 — 예산, 순서, 재개.

복수 조회가 사라져 1엔티티=1콜이다. 이 파일이 지키는 건 **예산을 정확히
지킨다**, **재생 횟수가 많은 것부터**, **404 를 다시 묻지 않는다**.
"""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
import respx

from spotify_taste._common import now_ms
from spotify_taste.clock import FrozenClock
from spotify_taste.config import Settings
from spotify_taste.db.connection import tx
from spotify_taste.metadata.backfill import enqueue_from_sessions, project_queue, run_backfill
from spotify_taste.spotify.auth import TokenProvider
from spotify_taste.spotify.client import SpotifyClient
from spotify_taste.spotify.endpoints import API_BASE
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

T0 = 1_800_000_000_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, spotify_client_id="cid", spotify_token_path=tmp_path / "t.json"
    )
    TokenStore(path=settings.spotify_token_path).save(
        TokenBundle(
            client_id="cid",
            access_token="TOK",
            refresh_token="REF",
            expires_at_ms=now_ms() + 3_600_000,
            obtained_at_ms=0,
        )
    )
    clock = FrozenClock(start_ms=now_ms(), start_monotonic=10.0)

    async def no_sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    monkeypatch.setattr("spotify_taste.spotify.client.asyncio.sleep", no_sleep)
    return SpotifyClient(
        token_provider=TokenProvider(settings=settings, store=store_of(settings), clock=clock),
        limiter=RateLimiter(clock=clock, sleep=no_sleep),
        settings=settings,
    )


def store_of(settings) -> TokenStore:
    return TokenStore(path=settings.spotify_token_path)


def _session(db: sqlite3.Connection, track_uri: str, *, plays: int = 1) -> None:
    with tx(db):
        for i in range(plays):
            db.execute(
                "INSERT INTO play_session (track_uri, started_at, source, timing_confidence, "
                "created_at, updated_at) VALUES (?, ?, 'poller', 'high', ?, ?)",
                (track_uri, T0 + i * 1_000, T0, T0),
            )


def _track_body(track_id: str, *, artist="ART1", album="ALB1") -> dict:
    return {
        "id": track_id,
        "name": f"곡 {track_id}",
        "duration_ms": 180_000,
        "explicit": False,
        "disc_number": 1,
        "track_number": 3,
        "is_local": False,
        "external_ids": {"isrc": "KRA382500001"},
        "album": {"uri": f"spotify:album:{album}", "id": album, "name": "앨범"},
        "artists": [{"uri": f"spotify:artist:{artist}", "id": artist, "name": "아티스트"}],
    }


# --- 큐 -------------------------------------------------------------------


def test_sessions_populate_the_queue(db: sqlite3.Connection) -> None:
    _session(db, "spotify:track:T1")
    enqueue_from_sessions(db)
    row = db.execute("SELECT uri, fetch_status FROM track").fetchone()
    assert (row["uri"], row["fetch_status"]) == ("spotify:track:T1", "pending")


def test_enqueue_is_idempotent(db: sqlite3.Connection) -> None:
    _session(db, "spotify:track:T1")
    enqueue_from_sessions(db)
    enqueue_from_sessions(db)
    assert db.execute("SELECT count(*) FROM track").fetchone()[0] == 1


# --- 해결 -----------------------------------------------------------------


@respx.mock
async def test_track_metadata_is_stored(db: sqlite3.Connection, client) -> None:
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(
        return_value=httpx.Response(200, json=_track_body("T1"))
    )
    respx.get(url__regex=rf"{API_BASE}/(artists|albums)/.*").mock(
        return_value=httpx.Response(200, json={"id": "X", "name": "n", "genres": ["k-indie"]})
    )
    result = await run_backfill(db, client, max_calls=10)

    row = db.execute("SELECT * FROM track WHERE uri = 'spotify:track:T1'").fetchone()
    assert row["fetch_status"] == "ok"
    assert row["isrc"] == "KRA382500001"
    assert row["duration_ms"] == 180_000
    assert result.tracks == 1
    await client.aclose()


@respx.mock
async def test_track_response_enqueues_artist_and_album(db: sqlite3.Connection, client) -> None:
    """복수 조회가 없으니 트랙 응답이 다음 큐를 만들어 준다."""
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(
        return_value=httpx.Response(200, json=_track_body("T1"))
    )
    respx.get(url__regex=rf"{API_BASE}/(artists|albums)/.*").mock(
        return_value=httpx.Response(200, json={"id": "X", "name": "n", "genres": []})
    )
    await run_backfill(db, client, max_calls=10)
    assert db.execute("SELECT count(*) FROM artist").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM album").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM track_artist").fetchone()[0] == 1
    await client.aclose()


@respx.mock
async def test_artist_genres_stored_as_json(db: sqlite3.Connection, client) -> None:
    """genres 가 오면 그대로 담는다.

    2026-08-15 현재 API 는 이 필드를 주지 않아 실데이터는 전부 `[]` 다.
    되살아나거나 다른 경로로 채울 때를 대비해 파서 쪽은 유지한다.
    """
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(
        return_value=httpx.Response(200, json=_track_body("T1"))
    )
    respx.get(f"{API_BASE}/artists/ART1").mock(
        return_value=httpx.Response(
            200, json={"id": "ART1", "name": "혁오", "genres": ["k-indie", "korean rock"]}
        )
    )
    respx.get(url__regex=rf"{API_BASE}/albums/.*").mock(
        return_value=httpx.Response(200, json={"id": "ALB1", "name": "앨범"})
    )
    # 아티스트는 트랙 응답에서 발견되므로 첫 실행에는 큐에 없다. 아티스트
    # 우선 순서에서는 그들이 **다음 실행**에 해결된다 (야간 배치 기준 한 밤).
    await run_backfill(db, client, max_calls=10)
    await run_backfill(db, client, max_calls=10)
    genres = db.execute("SELECT genres_json FROM artist").fetchone()[0]
    assert json.loads(genres) == ["k-indie", "korean rock"]
    await client.aclose()


@respx.mock
async def test_album_release_date_stored(db: sqlite3.Connection, client) -> None:
    """popularity 가 제거된 지금 era 를 알 수 있는 유일한 신호."""
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(
        return_value=httpx.Response(200, json=_track_body("T1"))
    )
    respx.get(url__regex=rf"{API_BASE}/artists/.*").mock(
        return_value=httpx.Response(200, json={"id": "A", "genres": []})
    )
    respx.get(f"{API_BASE}/albums/ALB1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "ALB1",
                "name": "앨범",
                "release_date": "2017-09-02",
                "release_date_precision": "day",
            },
        )
    )
    await run_backfill(db, client, max_calls=10)
    row = db.execute("SELECT release_date, release_date_precision FROM album").fetchone()
    assert (row["release_date"], row["release_date_precision"]) == ("2017-09-02", "day")
    await client.aclose()


# --- 예산과 순서 ----------------------------------------------------------


@respx.mock
async def test_max_calls_is_honoured_exactly(db: sqlite3.Connection, client) -> None:
    for i in range(10):
        _session(db, f"spotify:track:T{i}")
    route = respx.get(url__regex=rf"{API_BASE}/tracks/.*").mock(
        return_value=httpx.Response(200, json=_track_body("X"))
    )
    result = await run_backfill(db, client, max_calls=3)
    assert result.calls == 3
    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_most_played_tracks_resolve_first(db: sqlite3.Connection, client) -> None:
    """실제로 듣는 트랙이 첫날 밤에 해결돼야 체감이 산다."""
    _session(db, "spotify:track:RARE", plays=1)
    _session(db, "spotify:track:FAVOURITE", plays=20)
    route = respx.get(url__regex=rf"{API_BASE}/tracks/.*").mock(
        return_value=httpx.Response(200, json=_track_body("X"))
    )
    await run_backfill(db, client, max_calls=1)
    assert "FAVOURITE" in str(route.calls[0].request.url)
    await client.aclose()


@respx.mock
async def test_backfill_resumes_across_runs(db: sqlite3.Connection, client) -> None:
    """상태가 DB 에 있으므로 중간에 죽어도 잃는 것이 없다."""
    for i in range(4):
        _session(db, f"spotify:track:T{i}")
    respx.get(url__regex=rf"{API_BASE}/tracks/.*").mock(
        return_value=httpx.Response(200, json=_track_body("X"))
    )
    respx.get(url__regex=rf"{API_BASE}/(artists|albums)/.*").mock(
        return_value=httpx.Response(200, json={"id": "X", "name": "n"})
    )
    # 예산 2 씩 나눠 돌린다. 순서(아티스트 우선)와 무관하게, 이미 해결된 것을
    # 다시 묻지 않으므로 반복하면 결국 전부 채워진다 — 그게 재개 가능의 뜻이다.
    before = db.execute("SELECT count(*) FROM track WHERE fetch_status='ok'").fetchone()[0]
    await run_backfill(db, client, max_calls=2)
    after = db.execute("SELECT count(*) FROM track WHERE fetch_status='ok'").fetchone()[0]
    assert after > before

    for _ in range(6):
        await run_backfill(db, client, max_calls=2)
    assert db.execute("SELECT count(*) FROM track WHERE fetch_status='ok'").fetchone()[0] == 4
    await client.aclose()


# --- 실패 처리 ------------------------------------------------------------


@respx.mock
async def test_404_marked_permanently_and_not_retried(db: sqlite3.Connection, client) -> None:
    """없는 트랙을 매일 밤 다시 묻는 것은 예산 낭비다."""
    _session(db, "spotify:track:GONE")
    route = respx.get(f"{API_BASE}/tracks/GONE").mock(
        return_value=httpx.Response(404, json={"error": {"status": 404}})
    )
    result = await run_backfill(db, client, max_calls=5)
    assert result.not_found == 1
    assert db.execute("SELECT fetch_status FROM track").fetchone()[0] == "not_found"

    await run_backfill(db, client, max_calls=5)
    assert route.call_count == 1  # 다시 묻지 않는다
    await client.aclose()


@respx.mock
async def test_transient_error_is_retried_next_run(db: sqlite3.Connection, client) -> None:
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(return_value=httpx.Response(500))
    result = await run_backfill(db, client, max_calls=5)
    assert result.errors == 1
    assert db.execute("SELECT fetch_status FROM track").fetchone()[0] == "error"

    respx.get(f"{API_BASE}/tracks/T1").mock(
        return_value=httpx.Response(200, json=_track_body("T1"))
    )
    respx.get(url__regex=rf"{API_BASE}/(artists|albums)/.*").mock(
        return_value=httpx.Response(200, json={"id": "X", "genres": []})
    )
    await run_backfill(db, client, max_calls=5)
    assert db.execute("SELECT fetch_status FROM track").fetchone()[0] == "ok"
    await client.aclose()


@respx.mock
async def test_attempts_are_counted(db: sqlite3.Connection, client) -> None:
    _session(db, "spotify:track:T1")
    respx.get(f"{API_BASE}/tracks/T1").mock(return_value=httpx.Response(500))
    await run_backfill(db, client, max_calls=5)
    await run_backfill(db, client, max_calls=5)
    assert db.execute("SELECT fetch_attempts FROM track").fetchone()[0] == 2
    await client.aclose()


@respx.mock
async def test_empty_queue_is_a_noop(db: sqlite3.Connection, client) -> None:
    result = await run_backfill(db, client, max_calls=100)
    assert result.calls == 0
    await client.aclose()


def test_status_counts_unqueued_session_tracks(db: sqlite3.Connection) -> None:
    """대량 임포트 직후 status 가 '대기 0' 이라고 거짓말하면 안 된다 —
    enqueue 는 backfill run 시점에 일어난다."""
    _session(db, "spotify:track:NEW1")
    _session(db, "spotify:track:NEW2")
    from spotify_taste.db.queries import SELECT_BACKFILL_STATUS

    row = dict(db.execute(SELECT_BACKFILL_STATUS).fetchone())
    assert row["tracks_unqueued"] == 2
    enqueue_from_sessions(db)
    row = dict(db.execute(SELECT_BACKFILL_STATUS).fetchone())
    assert row["tracks_unqueued"] == 0
    assert row["tracks_pending"] == 2


# --- 완료 시점 추정 -------------------------------------------------------


def _status(**over) -> dict:
    row = {
        "tracks_ok": 2_262,
        "tracks_pending": 8_788,
        "artists_ok": 140,
        "artists_pending": 1_944,
        "albums_ok": 0,
        "albums_pending": 1_993,
    }
    row.update(over)
    return row


def test_projection_uses_measured_throughput_not_budget() -> None:
    """예산으로 나누면 몇 배씩 낙관적이다 — dev 모드 쿼터가 하루 600 근처에서
    끊는 것이 정상이라 예산 3,000 은 도달한 적이 없다 (2026-08-15 실측)."""
    p = project_queue(
        _status(), unqueued=0, resolved_recent=2_404, nights_recent=4, budget_per_night=3_000
    )
    assert p.measured
    assert p.per_night == 601.0
    # 예산 기준이었다면 5밤. 실측 기준이면 그 몇 배다.
    assert p.nights > 30


def test_projection_counts_queue_growth_from_pending_tracks() -> None:
    """트랙을 풀면 그 응답에서 아티스트·앨범이 큐에 오른다. 이걸 빼면
    '남은 5일' 같은 거짓말이 된다 — 실제로는 큐가 매일 늘고 있었다."""
    p = project_queue(
        _status(), unqueued=0, resolved_recent=600, nights_recent=1, budget_per_night=3_000
    )
    assert p.pending == 12_725
    assert p.growth > 0
    assert p.total > p.pending


def test_projection_falls_back_to_budget_without_history() -> None:
    """첫 실행에는 실적이 없다. 그래도 숫자는 내되 가정을 드러낸다."""
    p = project_queue(
        _status(tracks_ok=0, artists_ok=0, artists_pending=0, albums_pending=0),
        unqueued=0,
        resolved_recent=0,
        nights_recent=0,
        budget_per_night=3_000,
    )
    assert not p.measured
    assert p.per_night == 3_000
    assert p.growth == 0


def test_projection_counts_unqueued_tracks_as_work() -> None:
    """아직 큐에 안 올라온 트랙도 결국 콜이다 (대량 임포트 직후 상황)."""
    p = project_queue(
        _status(), unqueued=500, resolved_recent=600, nights_recent=1, budget_per_night=3_000
    )
    assert p.pending == 13_225


def test_projection_survives_sub_one_throughput() -> None:
    """처리량이 1건/밤 미만이면 예전 계산은 0 으로 나눠 죽었다.
    백필이 며칠 연속 거의 실패하면(쿼터 정책 변경·인증 만료) 실제로 그 값이다."""
    p = project_queue(
        _status(), unqueued=0, resolved_recent=1, nights_recent=3, budget_per_night=3_000
    )
    assert 0 < p.per_night < 1
    assert p.nights > p.total  # 하루 한 건도 못 푸는 상태 — 숫자가 크게 나와야 맞다


def test_projection_rounds_nights_up() -> None:
    """반 밤은 없다. 남은 일감이 조금이라도 있으면 한 밤 더다."""
    p = project_queue(
        _status(tracks_ok=0, artists_ok=0, artists_pending=0, albums_pending=0, tracks_pending=601),
        unqueued=0,
        resolved_recent=600,
        nights_recent=1,
        budget_per_night=3_000,
    )
    assert p.total == 601
    assert p.nights == 2


@respx.mock
async def test_artists_drain_before_tracks(db: sqlite3.Connection, client) -> None:
    """아티스트가 트랙보다 먼저다 (2026-08-20 변경).

    트랙 큐가 8천 건을 넘기면서 매일 예산 600 을 트랙이 전부 먹어 아티스트가
    나흘 연속 0명이었다. 그 결과 취향 벡터 상위 아티스트에 이름조차 없었다.
    """
    for i in range(5):
        _session(db, f"spotify:track:T{i}")
    with tx(db):
        for i in range(3):
            db.execute(
                "INSERT INTO artist (uri, fetch_status) VALUES (?, 'pending')",
                (f"spotify:artist:A{i}",),
            )
    tracks = respx.get(url__regex=rf"{API_BASE}/tracks/.*").mock(
        return_value=httpx.Response(200, json=_track_body("X"))
    )
    artists = respx.get(url__regex=rf"{API_BASE}/artists/.*").mock(
        return_value=httpx.Response(200, json={"id": "A", "name": "n"})
    )

    result = await run_backfill(db, client, max_calls=3)
    assert (artists.call_count, tracks.call_count) == (3, 0)
    assert result.artists == 3
    await client.aclose()


@respx.mock
async def test_budget_flows_to_tracks_once_artists_are_done(db: sqlite3.Connection, client) -> None:
    """아티스트 큐가 마르면 예산이 저절로 트랙으로 흐른다 — 비율을 손으로
    정할 필요가 없는 이유다."""
    for i in range(5):
        _session(db, f"spotify:track:T{i}")
    with tx(db):
        db.execute("INSERT INTO artist (uri, fetch_status) VALUES ('spotify:artist:A0', 'pending')")
    tracks = respx.get(url__regex=rf"{API_BASE}/tracks/.*").mock(
        return_value=httpx.Response(200, json=_track_body("X"))
    )
    respx.get(url__regex=rf"{API_BASE}/artists/.*").mock(
        return_value=httpx.Response(200, json={"id": "A", "name": "n"})
    )

    await run_backfill(db, client, max_calls=4)
    assert tracks.call_count == 3  # 아티스트 1 + 트랙 3
    await client.aclose()
