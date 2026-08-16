"""플레이리스트·라이브러리 스냅샷.

이 파일이 존재하는 이유: `snapshot_playlist_items()` 는 구현돼 있었는데
**아무도 부르지 않아** `playlist_item` 이 계속 비어 있었고, 그 결과 분류기의
`null_ctx_own_playlist` 규칙이 한 번도 발동하지 못했다 (2026-08-16 발견).
배선이 끊긴 것을 잡아 주는 테스트가 없어서 생긴 구멍이라 여기서 막는다.
"""

from __future__ import annotations

import sqlite3

import httpx
import pytest
import respx

from spotify_taste._common import now_ms
from spotify_taste.collect.snapshots import snapshot_own_playlist_items
from spotify_taste.config import Settings
from spotify_taste.db.connection import tx
from spotify_taste.spotify.auth import TokenProvider
from spotify_taste.spotify.client import SpotifyClient
from spotify_taste.spotify.endpoints import API_BASE
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

ME = "31vltpu7jit4xtjcike6wymykuuu"


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

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("spotify_taste.spotify.client.asyncio.sleep", no_sleep)
    return SpotifyClient(
        token_provider=TokenProvider(settings=settings, store=store),
        limiter=RateLimiter(sleep=no_sleep),
        settings=settings,
    )


def _seed(db: sqlite3.Connection, *, generated: int = 0, owner: str = ME) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO me (id, display_name, country, product, fetched_at) "
            "VALUES (?, 'JJ', 'KR', 'premium', ?)",
            (ME, now_ms()),
        )
        db.execute(
            "INSERT INTO playlist_meta (uri, id, name, owner_id, is_generated, "
            "fetched_at, fetch_status, refresh_after) "
            "VALUES (?, 'PL1', 'Current', ?, ?, ?, 'ok', ?)",
            ("spotify:playlist:PL1", owner, generated, now_ms(), now_ms() + 86_400_000),
        )


def _items_body() -> dict:
    return {
        "items": [
            {"item": {"uri": "spotify:track:T1"}, "added_at": "2026-01-01T00:00:00Z"},
            {"item": {"uri": "spotify:track:T2"}, "added_at": "2026-01-02T00:00:00Z"},
        ],
        "next": None,
    }


@respx.mock
async def test_own_playlist_items_are_stored(db: sqlite3.Connection, client) -> None:
    _seed(db)
    respx.get(f"{API_BASE}/playlists/PL1/items").mock(
        return_value=httpx.Response(200, json=_items_body())
    )
    stored = await snapshot_own_playlist_items(db, client)
    assert stored == 2
    uris = [r[0] for r in db.execute("SELECT track_uri FROM playlist_item ORDER BY position")]
    assert uris == ["spotify:track:T1", "spotify:track:T2"]
    await client.aclose()


@respx.mock
async def test_generated_playlists_are_skipped(db: sqlite3.Connection, client) -> None:
    """Spotify 생성분은 `/playlists/{id}` 가 404 고, 자발성 증거도 되지 못한다."""
    _seed(db, generated=1)
    route = respx.get(f"{API_BASE}/playlists/PL1/items").mock(
        return_value=httpx.Response(200, json=_items_body())
    )
    assert await snapshot_own_playlist_items(db, client) == 0
    assert route.call_count == 0
    await client.aclose()


@respx.mock
async def test_other_peoples_playlists_are_skipped(db: sqlite3.Connection, client) -> None:
    _seed(db, owner="someone_else")
    route = respx.get(f"{API_BASE}/playlists/PL1/items").mock(
        return_value=httpx.Response(200, json=_items_body())
    )
    assert await snapshot_own_playlist_items(db, client) == 0
    assert route.call_count == 0
    await client.aclose()


async def test_missing_me_row_does_not_crash(db: sqlite3.Connection, client) -> None:
    """`snapshot me` 를 먼저 안 돌린 상태. 경고만 남기고 조용히 0 을 낸다."""
    assert await snapshot_own_playlist_items(db, client) == 0
    await client.aclose()


def test_snapshot_all_wires_playlist_items(monkeypatch) -> None:
    """`snapshot all` 이 수록곡을 빠뜨리면 분류기가 반쪽으로 돈다.

    구현이 있는데 호출되지 않는 상태가 실제로 있었다 — 그걸 잡는 테스트다.
    """
    from typer.testing import CliRunner

    from spotify_taste.cli import app

    called: list[str] = []

    class _FakeCollector:
        conn = object()
        client = object()

        async def aclose(self) -> None:
            return None

    def _spy(name: str):
        async def _fn(conn, client, *args, **kwargs):
            called.append(name)
            return 0

        return _fn

    monkeypatch.setattr("spotify_taste.cli._backfill.build_collector", lambda: _FakeCollector())
    for fn in ("snapshot_me", "snapshot_library", "snapshot_playlists", "snapshot_top"):
        monkeypatch.setattr(f"spotify_taste.cli._backfill.{fn}", _spy(fn))
    monkeypatch.setattr(
        "spotify_taste.cli._backfill.snapshot_own_playlist_items", _spy("playlist_items")
    )

    result = CliRunner().invoke(app, ["snapshot", "all"])
    assert result.exit_code == 0, result.output
    assert "playlist_items" in called
