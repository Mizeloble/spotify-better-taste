"""HTTP 클라이언트 — 재시도 정책, 401 한 번, 429, 아카이브 훅.

핵심 불변식: **상태 변경 요청은 5xx 에 재시도하지 않는다**(곡이 두 번 들어간다),
그리고 **401 은 딱 한 번만 갱신 후 재시도한다**(무한 루프 금지).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from spotify_taste.clock import FrozenClock
from spotify_taste.config import Settings
from spotify_taste.spotify.auth import TOKEN_URL, TokenProvider
from spotify_taste.spotify.client import (
    MAX_RETRIES,
    SpotifyApiError,
    SpotifyClient,
    SpotifyResponse,
)
from spotify_taste.spotify.endpoints import API_BASE, Endpoint
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

NOW = 1_000_000_000_000
PLAYER_URL = f"{API_BASE}{Endpoint.PLAYER}"
TRACK_URL = f"{API_BASE}/tracks/abc"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """실제로 자지 않는 클라이언트."""
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
            scopes=("user-read-playback-state",),
            obtained_at_ms=NOW,
        )
    )
    clock = FrozenClock(start_ms=NOW, start_monotonic=1_000.0)

    async def no_sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    monkeypatch.setattr("spotify_taste.spotify.client.asyncio.sleep", no_sleep)

    provider = TokenProvider(settings=settings, store=store, clock=clock)
    limiter = RateLimiter(clock=clock, sleep=no_sleep, max_retry_after_sec=300.0)
    return SpotifyClient(token_provider=provider, limiter=limiter, settings=settings)


# --- 정상 경로 ------------------------------------------------------------


@respx.mock
async def test_get_returns_parsed_json(client: SpotifyClient) -> None:
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json={"is_playing": True}))
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None
    assert resp.json == {"is_playing": True}
    await client.aclose()


@respx.mock
async def test_bearer_token_is_sent(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json={}))
    await client.get(Endpoint.PLAYER)
    assert route.calls[0].request.headers["authorization"] == "Bearer TOK"
    await client.aclose()


@respx.mock
async def test_204_is_not_an_error(client: SpotifyClient) -> None:
    """`/me/player` 는 아무것도 재생하지 않을 때 204 를 준다 — 정상 상태다."""
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None
    assert resp.is_no_content
    assert resp.json is None
    await client.aclose()


@respx.mock
async def test_path_template_is_substituted_but_endpoint_key_kept(client: SpotifyClient) -> None:
    """아카이브 키는 템플릿이어야 '이 엔드포인트의 모양이 언제 바뀌었나'를
    조회할 수 있다."""
    respx.get(TRACK_URL).mock(return_value=httpx.Response(200, json={"id": "abc"}))
    resp = await client.get(Endpoint.TRACK, path_params={"id": "abc"})
    assert resp is not None
    assert resp.endpoint == "/tracks/{id}"
    await client.aclose()


@respx.mock
async def test_body_text_preserved_verbatim(client: SpotifyClient) -> None:
    """파싱된 dict 만 남기면 그 순간의 우리 가정이 섞인다."""
    raw = '{"id":"abc","name":"곡 이름"}'
    respx.get(TRACK_URL).mock(return_value=httpx.Response(200, content=raw.encode()))
    resp = await client.get(Endpoint.TRACK, path_params={"id": "abc"})
    assert resp is not None
    assert resp.body_text == raw
    await client.aclose()


# --- 재시도 정책 ----------------------------------------------------------


@respx.mock
async def test_5xx_retried_for_idempotent_request(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json={"ok": True}),
    ]
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None and resp.json == {"ok": True}
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_5xx_not_retried_for_state_changing_request(client: SpotifyClient) -> None:
    """이미 반영된 요청을 다시 보내면 곡이 두 번 들어간다."""
    route = respx.post(f"{API_BASE}/me/playlists").mock(return_value=httpx.Response(503))
    with pytest.raises(SpotifyApiError):
        await client.post(Endpoint.CREATE_PLAYLIST, json_body={"name": "x"})
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_retries_are_bounded(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(SpotifyApiError):
        await client.get(Endpoint.PLAYER)
    assert route.call_count == MAX_RETRIES + 1
    await client.aclose()


@respx.mock
async def test_network_error_retried_then_raised(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(SpotifyApiError):
        await client.get(Endpoint.PLAYER)
    assert route.call_count == MAX_RETRIES + 1
    await client.aclose()


@respx.mock
async def test_404_is_not_retried(client: SpotifyClient) -> None:
    """없는 트랙을 4번 물어봐야 여전히 없다 — 백필 예산만 태운다."""
    route = respx.get(TRACK_URL).mock(return_value=httpx.Response(404, json={"error": {}}))
    with pytest.raises(SpotifyApiError) as exc:
        await client.get(Endpoint.TRACK, path_params={"id": "abc"})
    assert exc.value.status == 404
    assert route.call_count == 1
    await client.aclose()


# --- 401 ------------------------------------------------------------------


@respx.mock
async def test_401_triggers_one_refresh_then_succeeds(client: SpotifyClient) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "FRESH", "expires_in": 3600})
    )
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(401, json={"error": {"message": "expired"}}),
        httpx.Response(200, json={"ok": True}),
    ]
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None and resp.json == {"ok": True}
    assert route.calls[1].request.headers["authorization"] == "Bearer FRESH"
    await client.aclose()


@respx.mock
async def test_repeated_401_does_not_loop_forever(client: SpotifyClient) -> None:
    """갱신 후에도 401 이면 만료가 아니라 scope 부족이나 계정 문제다."""
    refresh = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "FRESH", "expires_in": 3600})
    )
    route = respx.get(PLAYER_URL).mock(return_value=httpx.Response(401, json={"error": {}}))
    with pytest.raises(SpotifyApiError) as exc:
        await client.get(Endpoint.PLAYER)
    assert exc.value.status == 401
    assert refresh.call_count == 1  # 갱신은 딱 한 번
    assert route.call_count == 2
    await client.aclose()


# --- 429 ------------------------------------------------------------------


@respx.mock
async def test_429_retried_after_retry_after(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"ok": True}),
    ]
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None and resp.json == {"ok": True}
    await client.aclose()


@respx.mock
async def test_429_widens_the_shared_interval(client: SpotifyClient) -> None:
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={}),
    ]
    before = client.limiter.multiplier
    await client.get(Endpoint.PLAYER)
    assert client.limiter.multiplier > before
    await client.aclose()


@respx.mock
async def test_429_retried_even_for_state_changing_request(client: SpotifyClient) -> None:
    """429 는 '처리되지 않음'이므로 재시도해도 중복 생성이 아니다."""
    route = respx.post(f"{API_BASE}/me/playlists")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(201, json={"id": "pl"}),
    ]
    resp = await client.post(Endpoint.CREATE_PLAYLIST, json_body={"name": "x"})
    assert resp is not None and resp.status == 201
    await client.aclose()


@respx.mock
async def test_unparsable_retry_after_falls_back(client: SpotifyClient) -> None:
    """HTTP-date 형식이 올 수도 있다. 죽지 말고 기본값으로 간다."""
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(200, json={}),
    ]
    assert await client.get(Endpoint.PLAYER) is not None
    await client.aclose()


# --- 슬롯 획득 실패 -------------------------------------------------------


@respx.mock
async def test_slot_timeout_returns_none_without_calling(client: SpotifyClient) -> None:
    """폴러는 백필 뒤에 줄서지 않는다 — 틱을 건너뛰고 공백을 기록한다."""
    route = respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json={}))
    client.limiter.note_rate_limited(60.0)
    resp = await client.get(Endpoint.PLAYER, acquire_timeout=1.0)
    assert resp is None
    assert route.call_count == 0
    await client.aclose()


# --- 아카이브 훅 ----------------------------------------------------------


@respx.mock
async def test_archive_hook_receives_response_and_job(tmp_path, client: SpotifyClient) -> None:
    seen: list[tuple[SpotifyResponse, str]] = []
    client._archive = lambda resp, job: seen.append((resp, job))
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    await client.get(Endpoint.PLAYER, source_job="poller")
    assert len(seen) == 1
    assert seen[0][1] == "poller"
    assert seen[0][0].endpoint == Endpoint.PLAYER
    await client.aclose()


@respx.mock
async def test_archive_failure_does_not_break_the_request(client: SpotifyClient) -> None:
    """수집(저장)이 해석에 막히면 안 된다는 규칙의 HTTP 층 구현."""

    def boom(resp: SpotifyResponse, job: str) -> None:
        raise RuntimeError("disk full")

    client._archive = boom
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json={"a": 1}))
    resp = await client.get(Endpoint.PLAYER)
    assert resp is not None and resp.json == {"a": 1}
    await client.aclose()


@respx.mock
async def test_error_responses_are_archived_too(client: SpotifyClient) -> None:
    """404 의 원문도 남아야 나중에 '왜 못 찾았나'를 볼 수 있다."""
    seen: list[str] = []
    client._archive = lambda resp, job: seen.append(resp.endpoint)
    respx.get(TRACK_URL).mock(return_value=httpx.Response(404, json={"error": {}}))
    with pytest.raises(SpotifyApiError):
        await client.get(Endpoint.TRACK, path_params={"id": "abc"})
    assert seen == ["/tracks/{id}"]
    await client.aclose()


# --- 페이징 ---------------------------------------------------------------


@respx.mock
async def test_paginate_follows_next(client: SpotifyClient) -> None:
    route = respx.get(f"{API_BASE}{Endpoint.MY_TRACKS}")
    route.side_effect = [
        httpx.Response(200, json={"items": [{"i": 1}, {"i": 2}], "next": "more"}),
        httpx.Response(200, json={"items": [{"i": 3}], "next": None}),
    ]
    items = await client.paginate(Endpoint.MY_TRACKS, limit=2)
    assert [i["i"] for i in items] == [1, 2, 3]
    await client.aclose()


@respx.mock
async def test_paginate_stops_on_empty_page(client: SpotifyClient) -> None:
    respx.get(f"{API_BASE}{Endpoint.MY_TRACKS}").mock(
        return_value=httpx.Response(200, json={"items": [], "next": "more"})
    )
    assert await client.paginate(Endpoint.MY_TRACKS) == []
    await client.aclose()


@respx.mock
async def test_paginate_is_bounded(client: SpotifyClient) -> None:
    """`next` 가 영원히 나와도 무한 루프가 아니라 잘린 결과여야 한다."""
    respx.get(f"{API_BASE}{Endpoint.MY_TRACKS}").mock(
        return_value=httpx.Response(200, json={"items": [{"i": 1}], "next": "forever"})
    )
    items = await client.paginate(Endpoint.MY_TRACKS, max_pages=3)
    assert len(items) == 3
    await client.aclose()


@respx.mock
async def test_paginate_supports_renamed_items_key(client: SpotifyClient) -> None:
    """2026-02: /playlists/{id}/tracks → /items, 응답 키도 함께 바뀌었다."""
    respx.get(f"{API_BASE}/playlists/pl/items").mock(
        return_value=httpx.Response(200, json={"items": [{"item": {"uri": "u"}}], "next": None})
    )
    items = await client.paginate(
        Endpoint.PLAYLIST_ITEMS, path_params={"id": "pl"}, items_key="items"
    )
    assert items == [{"item": {"uri": "u"}}]
    await client.aclose()
