"""보조 폴러 — 증분 커서와 응답 파싱.

이 경로는 주 폴러를 **대체하지 않는다**. ms_played 가 없고 played_at 이
30초로 반올림돼 오므로, 만들어 내는 세션은 전부 저신뢰다.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from spotify_taste._common import iso_to_epoch_ms
from spotify_taste.clock import FrozenClock
from spotify_taste.collect.recent_poller import CURSOR_NAME, RecentPoller
from spotify_taste.config import Settings
from spotify_taste.db.queries import GET_CURSOR
from spotify_taste.spotify.auth import TokenProvider
from spotify_taste.spotify.client import SpotifyClient
from spotify_taste.spotify.endpoints import API_BASE, Endpoint
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.raw_archive import RawArchive
from spotify_taste.spotify.schemas import parse_recently_played
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

RP_URL = f"{API_BASE}{Endpoint.RECENTLY_PLAYED}"
NOW = 1_800_000_000_000
A = "spotify:track:AAAA"
B = "spotify:track:BBBB"


def _item(track_uri: str, played_at_iso: str, *, duration: int = 180_000, ctx=None) -> dict:
    entry: dict = {
        "track": {"uri": track_uri, "duration_ms": duration, "name": "곡"},
        "played_at": played_at_iso,
    }
    entry["context"] = {"uri": ctx, "type": "playlist"} if ctx else None
    return entry


@pytest.fixture
def poller(db: sqlite3.Connection, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        spotify_client_id="cid",
        spotify_token_path=tmp_path / "token.json",
    )
    store = TokenStore(path=settings.spotify_token_path)
    store.save(
        TokenBundle(
            client_id="cid",
            access_token="TOK",
            refresh_token="REF",
            expires_at_ms=NOW + 3_600_000,
            obtained_at_ms=NOW,
        )
    )
    clock = FrozenClock(start_ms=NOW, start_monotonic=100.0)

    async def no_sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    monkeypatch.setattr("spotify_taste.spotify.client.asyncio.sleep", no_sleep)
    provider = TokenProvider(settings=settings, store=store, clock=clock)
    client = SpotifyClient(
        token_provider=provider,
        limiter=RateLimiter(clock=clock, sleep=no_sleep),
        settings=settings,
    )
    return (
        RecentPoller(
            conn=db, client=client, archive=RawArchive(db, settings=settings), settings=settings
        ),
        db,
        client,
    )


# --- 파싱 -----------------------------------------------------------------


def test_parses_items_in_any_order() -> None:
    plays = parse_recently_played(
        {"items": [_item(A, "2026-08-09T12:00:00Z"), _item(B, "2026-08-09T12:03:00Z")]}
    )
    assert [p.track_uri for p in plays] == [A, B]
    assert plays[1].played_at - plays[0].played_at == 180_000


def test_entry_without_played_at_is_skipped() -> None:
    """재생하지 않은 곡이 중복 항목으로 들어오는 사례가 보고돼 있다."""
    plays = parse_recently_played(
        {"items": [{"track": {"uri": A}}, _item(B, "2026-08-09T12:00:00Z")]}
    )
    assert [p.track_uri for p in plays] == [B]


def test_entry_without_track_is_skipped() -> None:
    plays = parse_recently_played({"items": [{"played_at": "2026-08-09T12:00:00Z"}]})
    assert plays == []


def test_malformed_timestamp_is_skipped_not_fatal() -> None:
    plays = parse_recently_played({"items": [_item(A, "어제쯤")]})
    assert plays == []


def test_null_context_tolerated() -> None:
    """Liked Songs 재생은 정상적으로도 context 가 null 이다."""
    plays = parse_recently_played({"items": [_item(A, "2026-08-09T12:00:00Z")]})
    assert plays[0].context_uri is None


def test_empty_and_garbage_bodies() -> None:
    assert parse_recently_played({}) == []
    assert parse_recently_played({"items": []}) == []
    assert parse_recently_played(None) == []
    assert parse_recently_played({"items": [None, 3, "x"]}) == []


# --- 커서 -----------------------------------------------------------------


@respx.mock
async def test_first_poll_sends_no_cursor(poller) -> None:
    p, _, client = poller
    route = respx.get(RP_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    await p.poll_once()
    assert "after" not in route.calls[0].request.url.params
    await client.aclose()


@respx.mock
async def test_cursor_advances_to_latest_played_at(poller) -> None:
    p, db, client = poller
    respx.get(RP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"items": [_item(A, "2026-08-09T12:00:00Z"), _item(B, "2026-08-09T12:03:00Z")]},
        )
    )
    await p.poll_once()
    cursor = int(db.execute(GET_CURSOR, (CURSOR_NAME,)).fetchone()[0])
    # 커서는 페이지에서 **가장 늦은** played_at 이어야 한다. 더 이른 값을 쓰면
    # 이미 처리한 구간을 매번 다시 읽는다.
    assert cursor == iso_to_epoch_ms("2026-08-09T12:03:00Z")
    await client.aclose()


@respx.mock
async def test_second_poll_sends_stored_cursor(poller) -> None:
    p, _, client = poller
    respx.get(RP_URL).mock(
        return_value=httpx.Response(200, json={"items": [_item(A, "2026-08-09T12:00:00Z")]})
    )
    await p.poll_once()

    route = respx.get(RP_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    await p.poll_once()
    # respx 는 같은 패턴이면 라우트를 재사용하므로 calls[0] 은 첫 요청이다.
    last = route.calls[-1].request
    assert last.url.params["after"] == str(iso_to_epoch_ms("2026-08-09T12:00:00Z"))
    await client.aclose()


@respx.mock
async def test_cursor_not_advanced_on_fetch_failure(poller) -> None:
    """실패했는데 커서를 옮기면 그 구간이 영원히 비어 있게 된다."""
    p, db, client = poller
    respx.get(RP_URL).mock(return_value=httpx.Response(500))
    await p.poll_once()
    assert db.execute(GET_CURSOR, (CURSOR_NAME,)).fetchone() is None
    await client.aclose()


@respx.mock
async def test_empty_page_does_not_move_cursor(poller) -> None:
    p, db, client = poller
    respx.get(RP_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    await p.poll_once()
    assert db.execute(GET_CURSOR, (CURSOR_NAME,)).fetchone() is None
    await client.aclose()


# --- 병합 연동 ------------------------------------------------------------


@respx.mock
async def test_poll_inserts_low_confidence_sessions(poller) -> None:
    p, db, client = poller
    respx.get(RP_URL).mock(
        return_value=httpx.Response(
            200,
            json={"items": [_item(A, "2026-08-09T12:00:00Z"), _item(B, "2026-08-09T12:03:00Z")]},
        )
    )
    stats = await p.poll_once()
    assert stats.inserted == 2
    confidences = {r[0] for r in db.execute("SELECT timing_confidence FROM play_session")}
    assert confidences == {"low"}
    await client.aclose()


@respx.mock
async def test_repeated_poll_does_not_duplicate(poller) -> None:
    """같은 페이지를 다시 읽어도 행이 늘어나면 안 된다."""
    p, db, client = poller
    respx.get(RP_URL).mock(
        return_value=httpx.Response(200, json={"items": [_item(A, "2026-08-09T12:00:00Z")]})
    )
    await p.poll_once()
    await p.poll_once()
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1
    await client.aclose()


@respx.mock
async def test_rate_limit_skip_is_harmless(poller) -> None:
    """급할 것 없는 보조 경로다 — 다음 주기에 다시 읽으면 된다."""
    p, db, client = poller
    respx.get(RP_URL).mock(return_value=httpx.Response(200, json={"items": []}))
    client.limiter.note_rate_limited(120.0)
    stats = await p.poll_once()
    assert (stats.inserted, stats.confirmed) == (0, 0)
    assert db.execute(GET_CURSOR, (CURSOR_NAME,)).fetchone() is None
    await client.aclose()
