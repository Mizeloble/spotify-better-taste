"""분류 재계산과 플레이리스트 캐시 TTL.

지키는 불변식: **dry-run 은 아무것도 쓰지 않는다**, **RULES_VERSION 이 오르면
전 행이 대상이 된다**, **실패한 조회도 캐시된다**.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from spotify_taste._common import now_ms
from spotify_taste.classify import patterns
from spotify_taste.classify.playlist_cache import (
    TTL_ERROR_MS,
    TTL_MISSING_MS,
    TTL_OK_MS,
    refresh_playlist_cache,
)
from spotify_taste.classify.recompute import recompute
from spotify_taste.clock import FrozenClock
from spotify_taste.config import Settings
from spotify_taste.db.connection import tx
from spotify_taste.spotify.auth import TokenProvider
from spotify_taste.spotify.client import SpotifyClient
from spotify_taste.spotify.endpoints import API_BASE
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

ME = "jjmize"
T0 = 1_800_000_000_000
A = "spotify:track:AAAA"
PL = "spotify:playlist:PL123"


def _seed_me(db: sqlite3.Connection) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO me (id, display_name, product, fetched_at) VALUES (?, 'me', 'premium', ?)",
            (ME, T0),
        )


def _session(db: sqlite3.Connection, **over) -> int:
    row: dict = {
        "track_uri": A,
        "started_at": T0,
        "ended_at": T0 + 180_000,
        "source": "poller",
        "timing_confidence": "high",
        "created_at": T0,
        "updated_at": T0,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(["?"] * len(row))
    with tx(db):
        cur = db.execute(f"INSERT INTO play_session ({cols}) VALUES ({marks})", tuple(row.values()))
    return int(cur.lastrowid or 0)


def _playlist(db: sqlite3.Connection, *, owner: str, name="이름", generated=0, status="ok") -> None:
    with tx(db):
        db.execute(
            "INSERT INTO playlist_meta (uri, name, owner_id, is_generated, fetched_at, "
            "fetch_status, refresh_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (PL, name, owner, generated, T0, status, T0 + TTL_OK_MS),
        )


# --- dry-run --------------------------------------------------------------


def test_dry_run_writes_nothing(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner="spotify")

    result = recompute(db, write=False)
    assert result.considered == 1
    assert result.changed == 1
    assert db.execute("SELECT context_origin FROM play_session").fetchone()[0] is None


def test_dry_run_reports_transition_matrix(db: sqlite3.Connection) -> None:
    """규칙을 고치기 전에 '무엇이 어디로 옮겨 가는가'를 봐야 한다."""
    _seed_me(db)
    _session(db, context_uri=PL, context_type="playlist", context_origin="unknown")
    _playlist(db, owner="spotify")

    result = recompute(db, stale_only=False, write=False)
    assert result.transition_rows() == [("unknown", "algorithmic", 1)]


def test_write_persists_origin_and_reason(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner=ME)

    with tx(db):
        recompute(db, write=True)
    row = db.execute(
        "SELECT context_origin, context_origin_reason, context_origin_rules_version "
        "FROM play_session"
    ).fetchone()
    assert row["context_origin"] == "self"
    assert row["context_origin_reason"] == "playlist_owner_me"
    assert row["context_origin_rules_version"] == patterns.RULES_VERSION


# --- stale 선택 -----------------------------------------------------------


def test_stale_skips_current_version(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(
        db,
        context_uri=PL,
        context_type="playlist",
        context_origin="self",
        context_origin_rules_version=patterns.RULES_VERSION,
    )
    _playlist(db, owner=ME)
    assert recompute(db, stale_only=True, write=False).considered == 0


def test_bumping_rules_version_makes_everything_stale(db: sqlite3.Connection, monkeypatch) -> None:
    """이게 되기 때문에 폴러가 context_origin 을 건드리지 않는 것이다."""
    _seed_me(db)
    _session(
        db,
        context_uri=PL,
        context_type="playlist",
        context_origin="self",
        context_origin_rules_version=patterns.RULES_VERSION,
    )
    _playlist(db, owner=ME)

    monkeypatch.setattr(
        "spotify_taste.classify.recompute.RULES_VERSION", patterns.RULES_VERSION + 1
    )
    assert recompute(db, stale_only=True, write=False).considered == 1


def test_all_mode_reconsiders_everything(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(
        db,
        context_uri=PL,
        context_type="playlist",
        context_origin="self",
        context_origin_rules_version=patterns.RULES_VERSION,
    )
    _playlist(db, owner=ME)
    assert recompute(db, stale_only=False, write=False).considered == 1


def test_since_filters_by_start_time(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, started_at=T0, track_uri="spotify:track:OLD")
    _session(db, started_at=T0 + 86_400_000, track_uri="spotify:track:NEW")
    assert recompute(db, stale_only=False, since=T0 + 1, write=False).considered == 1


# --- 증거 수집 ------------------------------------------------------------


def test_library_membership_promotes_null_context(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db)  # context 없음
    with tx(db):
        db.execute("INSERT INTO library_track (track_uri, observed_at) VALUES (?, ?)", (A, T0))
    result = recompute(db, write=False)
    assert result.origins["self"] == 1


def test_null_context_without_evidence_stays_unknown(db: sqlite3.Connection) -> None:
    """오토플레이를 self 로 넣지 않는다."""
    _seed_me(db)
    _session(db)
    assert recompute(db, write=False).origins["unknown"] == 1


def test_own_generated_playlist_is_algorithmic(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner=ME, generated=1)
    assert recompute(db, write=False).origins["algorithmic"] == 1


def test_unresolved_playlist_is_unknown(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner=None, status="error")
    assert recompute(db, write=False).origins["unknown"] == 1


def test_silence_gap_computed_from_previous_session(db: sqlite3.Connection) -> None:
    """직전 재생이 끝난 뒤 오래 조용했으면 사람이 직접 튼 것으로 본다."""
    _seed_me(db)
    _session(db, track_uri="spotify:track:FIRST", started_at=T0, ended_at=T0 + 180_000)
    _session(db, track_uri=A, started_at=T0 + 180_000 + 600_000, shuffle=0)
    result = recompute(db, stale_only=False, write=False)
    assert result.origins["self"] >= 1


def test_missing_me_row_does_not_crash(db: sqlite3.Connection) -> None:
    """`snapshot me` 를 아직 안 돌린 상태에서도 죽지 않아야 한다."""
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner="누군가")
    assert recompute(db, write=False).considered == 1


def test_degraded_mode_drops_silence_heuristic(db: sqlite3.Connection) -> None:
    _seed_me(db)
    _session(db, track_uri="spotify:track:FIRST", started_at=T0, ended_at=T0 + 180_000)
    _session(db, track_uri=A, started_at=T0 + 180_000 + 600_000, shuffle=0)
    result = recompute(db, stale_only=False, write=False, degraded_mode=True)
    assert result.origins["self"] == 0


def test_superseded_sessions_are_not_classified(db: sqlite3.Connection) -> None:
    _seed_me(db)
    keep = _session(db, started_at=T0)
    _session(db, started_at=T0 + 1_000, superseded_by=keep)
    assert recompute(db, write=False).considered == 1


# --- 플레이리스트 캐시 ----------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, spotify_client_id="cid", spotify_token_path=tmp_path / "t.json"
    )
    store = TokenStore(path=settings.spotify_token_path)
    store.save(
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
        token_provider=TokenProvider(settings=settings, store=store, clock=clock),
        limiter=RateLimiter(clock=clock, sleep=no_sleep),
        settings=settings,
    )


@respx.mock
async def test_cache_resolves_owner(db: sqlite3.Connection, client) -> None:
    _session(db, context_uri=PL, context_type="playlist")
    respx.get(f"{API_BASE}/playlists/PL123").mock(
        return_value=httpx.Response(
            200,
            json={"id": "PL123", "name": "Discover Weekly", "owner": {"id": "spotify"}},
        )
    )
    assert await refresh_playlist_cache(db, client) == 1
    row = db.execute("SELECT owner_id, fetch_status FROM playlist_meta").fetchone()
    assert (row["owner_id"], row["fetch_status"]) == ("spotify", "ok")
    await client.aclose()


@respx.mock
async def test_404_is_cached_so_it_is_not_refetched_forever(db: sqlite3.Connection, client) -> None:
    """삭제된 플레이리스트를 매 분류마다 다시 조회하면 안 된다."""
    _session(db, context_uri=PL, context_type="playlist")
    route = respx.get(f"{API_BASE}/playlists/PL123").mock(
        return_value=httpx.Response(404, json={"error": {"status": 404}})
    )
    await refresh_playlist_cache(db, client)
    row = db.execute("SELECT fetch_status, refresh_after FROM playlist_meta").fetchone()
    assert row["fetch_status"] == "not_found"
    assert row["refresh_after"] > now_ms() + TTL_MISSING_MS - 60_000

    # TTL 이 남아 있으므로 두 번째 실행은 조회하지 않는다.
    await refresh_playlist_cache(db, client)
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_transient_error_uses_short_ttl(db: sqlite3.Connection, client) -> None:
    _session(db, context_uri=PL, context_type="playlist")
    respx.get(f"{API_BASE}/playlists/PL123").mock(return_value=httpx.Response(500))
    await refresh_playlist_cache(db, client)
    row = db.execute("SELECT fetch_status, refresh_after FROM playlist_meta").fetchone()
    assert row["fetch_status"] == "error"
    assert row["refresh_after"] < now_ms() + TTL_ERROR_MS + 60_000
    await client.aclose()


@respx.mock
async def test_resolved_playlist_is_not_refetched(db: sqlite3.Connection, client) -> None:
    _session(db, context_uri=PL, context_type="playlist")
    _playlist(db, owner="spotify")
    route = respx.get(f"{API_BASE}/playlists/PL123")
    await refresh_playlist_cache(db, client)
    assert route.call_count == 0
    await client.aclose()


@respx.mock
async def test_lookup_budget_is_respected(db: sqlite3.Connection, client) -> None:
    """분류가 갑자기 수백 콜을 쏘면 폴러가 굶는다."""
    for i in range(10):
        _session(
            db,
            track_uri=f"spotify:track:{i}",
            started_at=T0 + i * 1_000,
            context_uri=f"spotify:playlist:P{i}",
            context_type="playlist",
        )
    route = respx.get(url__regex=rf"{API_BASE}/playlists/.*").mock(
        return_value=httpx.Response(200, json={"id": "x", "owner": {"id": "spotify"}})
    )
    await refresh_playlist_cache(db, client, max_lookups=3)
    assert route.call_count == 3
    await client.aclose()


def test_malformed_uri_is_marked_not_found(db: sqlite3.Connection) -> None:
    from spotify_taste.classify.playlist_cache import _id_from_uri

    assert _id_from_uri("spotify:playlist:ABC") == "ABC"
    assert _id_from_uri("spotify:album:ABC") is None
    assert _id_from_uri("garbage") is None
