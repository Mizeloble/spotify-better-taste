"""토큰 갱신 — 회전 영속화, 폐기 처리, 동시성.

여기서 지키는 불변식: **회전된 refresh token 을 잃지 않는다**, 그리고
**폐기는 재시도하지 않고 즉시 사람을 부른다**.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from spotify_taste.clock import FrozenClock
from spotify_taste.config import Settings
from spotify_taste.notify import AlertKind
from spotify_taste.spotify.auth import (
    ALERT_AFTER_CONSECUTIVE_FAILURES,
    TOKEN_URL,
    SpotifyAuthError,
    SpotifyAuthRevoked,
    TokenProvider,
)
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

NOW = 1_000_000_000_000


class RecordingNotifier:
    """보낸 알림을 기록만 하는 테스트용 채널."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, kind: str, title: str, body: str = "") -> bool:
        self.sent.append((kind, title))
        return True

    def kinds(self) -> list[str]:
        return [k for k, _ in self.sent]


@pytest.fixture
def env(tmp_path: Path):
    settings = Settings(
        _env_file=None,
        spotify_client_id="cid",
        spotify_token_path=tmp_path / "token.json",
        heartbeat_path=tmp_path / "hb" / "collector.txt",
    )
    store = TokenStore(path=settings.spotify_token_path)
    notifier = RecordingNotifier()
    clock = FrozenClock(start_ms=NOW)
    return settings, store, notifier, clock


def _stored(store: TokenStore, **over) -> TokenBundle:
    fields: dict = {
        "client_id": "cid",
        "access_token": "OLD-ACCESS",
        "refresh_token": "OLD-REFRESH",
        "expires_at_ms": NOW - 1,  # 기본은 이미 만료된 상태
        "scopes": ("user-read-playback-state",),
        "obtained_at_ms": NOW - 3_600_000,
    }
    fields.update(over)
    bundle = TokenBundle(**fields)
    store.save(bundle)
    return bundle


def _provider(env, **over) -> TokenProvider:
    settings, store, notifier, clock = env
    return TokenProvider(settings=settings, store=store, notifier=notifier, clock=clock, **over)


# --- 정상 갱신 ------------------------------------------------------------


@respx.mock
async def test_refreshes_expired_token(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW-ACCESS", "expires_in": 3600})
    )
    p = _provider(env)
    assert await p.get_access_token() == "NEW-ACCESS"
    await p.aclose()


@respx.mock
async def test_valid_token_is_not_refreshed(env) -> None:
    _, store, _, _ = env
    _stored(store, expires_at_ms=NOW + 3_600_000)
    route = respx.post(TOKEN_URL)
    p = _provider(env)
    assert await p.get_access_token() == "OLD-ACCESS"
    assert not route.called
    await p.aclose()


@respx.mock
async def test_expiry_margin_refreshes_early(env) -> None:
    """만료 직전에 미리 갱신해야 폴러 한 틱이 401 로 낭비되지 않는다."""
    _, store, _, _ = env
    _stored(store, expires_at_ms=NOW + 60_000)  # 마진 120초 안쪽
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
    )
    p = _provider(env)
    await p.get_access_token()
    assert route.called
    await p.aclose()


@respx.mock
async def test_pkce_refresh_sends_no_client_secret(env) -> None:
    """PKCE 는 시크릿을 쓰지 않는다. 실려 나가면 설계가 깨진 것."""
    _, store, _, _ = env
    _stored(store)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "N", "expires_in": 3600})
    )
    p = _provider(env)
    await p.get_access_token()
    req = route.calls[0].request
    body = req.content.decode()
    assert "client_secret" not in body
    assert "authorization" not in {k.lower() for k in req.headers}
    assert "client_id=cid" in body
    await p.aclose()


# --- refresh token 회전 ---------------------------------------------------


@respx.mock
async def test_rotated_refresh_token_is_persisted_immediately(env) -> None:
    """저장 전에 프로세스가 죽으면 재인증이다 — 이 프로젝트에서 수집기를
    단일 프로세스로 묶은 이유(C1)와 같은 뿌리."""
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW-ACCESS",
                "refresh_token": "ROTATED-REFRESH",
                "expires_in": 3600,
            },
        )
    )
    p = _provider(env)
    await p.get_access_token()
    # 새 프로세스가 읽어도 회전된 값이 보여야 한다.
    assert TokenStore(path=store.path).load().refresh_token == "ROTATED-REFRESH"
    await p.aclose()


@respx.mock
async def test_absent_refresh_token_keeps_previous(env) -> None:
    """갱신 응답은 refresh_token 을 생략할 수 있다."""
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
    )
    p = _provider(env)
    await p.get_access_token()
    assert store.load().refresh_token == "OLD-REFRESH"
    await p.aclose()


@respx.mock
async def test_scopes_preserved_when_response_omits_them(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
    )
    p = _provider(env)
    await p.get_access_token()
    assert store.load().scopes == ("user-read-playback-state",)
    await p.aclose()


@respx.mock
async def test_scopes_updated_when_response_provides_them(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "N", "expires_in": 3600, "scope": "a b c"}
        )
    )
    p = _provider(env)
    await p.get_access_token()
    assert store.load().scopes == ("a", "b", "c")
    await p.aclose()


# --- 폐기 (invalid_grant) -------------------------------------------------


@respx.mock
async def test_invalid_grant_raises_revoked(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked):
        await p.get_access_token()
    await p.aclose()


@respx.mock
async def test_invalid_grant_is_not_retried(env) -> None:
    """사람이 재인증해야만 풀리는 상태다. 재시도는 알림만 늦춘다."""
    _, store, _, _ = env
    _stored(store)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked):
        await p.get_access_token()
    assert route.call_count == 1
    await p.aclose()


@respx.mock
async def test_invalid_grant_alerts_immediately(env) -> None:
    _, store, notifier, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked):
        await p.get_access_token()
    assert AlertKind.AUTH_REVOKED in notifier.kinds()
    await p.aclose()


@respx.mock
async def test_invalid_grant_writes_reauth_sentinel(env) -> None:
    """launchd 래퍼가 이 파일을 보고 exit 0 해야 재시작 폭풍이 안 난다."""
    settings, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked):
        await p.get_access_token()
    sentinel = settings.reauth_sentinel_path
    assert sentinel.exists()
    assert "auth login" in sentinel.read_text()
    await p.aclose()


@respx.mock
async def test_invalid_grant_invalidates_stored_bundle(env) -> None:
    """폐기된 번들을 들고 있으면 재시작마다 같은 실패를 반복한다."""
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked):
        await p.get_access_token()
    assert store.load() is None
    await p.aclose()


@respx.mock
async def test_missing_token_raises_revoked_with_actionable_message(env) -> None:
    respx.post(TOKEN_URL)
    p = _provider(env)
    with pytest.raises(SpotifyAuthRevoked, match="auth login"):
        await p.get_access_token()
    await p.aclose()


# --- 일시적 실패 ----------------------------------------------------------


@respx.mock
async def test_server_error_raises_recoverable_error(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    p = _provider(env)
    with pytest.raises(SpotifyAuthError) as exc:
        await p.get_access_token()
    assert not isinstance(exc.value, SpotifyAuthRevoked)
    await p.aclose()


@respx.mock
async def test_network_error_raises_recoverable_error(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    p = _provider(env)
    with pytest.raises(SpotifyAuthError):
        await p.get_access_token()
    await p.aclose()


@respx.mock
async def test_transient_failures_alert_only_after_threshold(env) -> None:
    """네트워크가 잠깐 끊길 때마다 알림이 오면 아무도 안 본다."""
    _, store, notifier, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    p = _provider(env)

    for _ in range(ALERT_AFTER_CONSECUTIVE_FAILURES - 1):
        with pytest.raises(SpotifyAuthError):
            await p.get_access_token()
    assert AlertKind.AUTH_REFRESH_FAILING not in notifier.kinds()

    with pytest.raises(SpotifyAuthError):
        await p.get_access_token()
    assert AlertKind.AUTH_REFRESH_FAILING in notifier.kinds()
    await p.aclose()


@respx.mock
async def test_success_resets_failure_counter(env) -> None:
    _, store, notifier, _ = env
    _stored(store)
    route = respx.post(TOKEN_URL)
    route.mock(return_value=httpx.Response(503))

    p = _provider(env)
    for _ in range(ALERT_AFTER_CONSECUTIVE_FAILURES - 1):
        with pytest.raises(SpotifyAuthError):
            await p.get_access_token()

    route.mock(return_value=httpx.Response(200, json={"access_token": "N", "expires_in": 3600}))
    await p.get_access_token()

    route.mock(return_value=httpx.Response(503))
    p._bundle = None  # 만료된 상태로 되돌려 다시 갱신을 시도하게 한다
    _stored(store)
    with pytest.raises(SpotifyAuthError):
        await p.get_access_token()
    assert AlertKind.AUTH_REFRESH_FAILING not in notifier.kinds()
    await p.aclose()


@respx.mock
async def test_malformed_json_is_recoverable_not_revoked(env) -> None:
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, content=b"<html>oops"))
    p = _provider(env)
    with pytest.raises(SpotifyAuthError) as exc:
        await p.get_access_token()
    assert not isinstance(exc.value, SpotifyAuthRevoked)
    await p.aclose()


@respx.mock
async def test_other_400_error_is_not_treated_as_revoked(env) -> None:
    """invalid_request 는 우리 쪽 버그지 토큰 폐기가 아니다."""
    _, store, _, _ = env
    _stored(store)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_request"}))
    p = _provider(env)
    with pytest.raises(SpotifyAuthError) as exc:
        await p.get_access_token()
    assert not isinstance(exc.value, SpotifyAuthRevoked)
    await p.aclose()


# --- 동시성 ---------------------------------------------------------------


@respx.mock
async def test_concurrent_callers_refresh_only_once(env) -> None:
    """폴러와 백필이 동시에 토큰을 물으면 갱신이 두 번 일어나 회전된
    refresh token 이 서로를 무효화할 수 있다."""
    _, store, _, _ = env
    _stored(store)
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 3600})
    )
    p = _provider(env)
    results = await asyncio.gather(*(p.get_access_token() for _ in range(5)))
    assert results == ["NEW"] * 5
    assert route.call_count == 1
    await p.aclose()


# --- 코드 교환 ------------------------------------------------------------


@respx.mock
async def test_exchange_code_saves_bundle_and_clears_sentinel(env) -> None:
    settings, store, _, _ = env
    settings.reauth_sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    settings.reauth_sentinel_path.write_text("stale")

    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "A",
                "refresh_token": "R",
                "expires_in": 3600,
                "scope": "user-top-read",
            },
        )
    )
    p = _provider(env)
    bundle = await p.exchange_code(code="CODE", verifier="VERIFIER")

    assert (bundle.access_token, bundle.refresh_token) == ("A", "R")
    assert store.load().refresh_token == "R"
    assert not settings.reauth_sentinel_path.exists()

    body = route.calls[0].request.content.decode()
    assert "grant_type=authorization_code" in body
    assert "code_verifier=VERIFIER" in body
    assert "client_secret" not in body
    await p.aclose()


@respx.mock
async def test_exchange_code_without_refresh_token_fails_loudly(env) -> None:
    """refresh token 없는 번들을 저장하면 한 시간 뒤 조용히 죽는다."""
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "A", "expires_in": 3600})
    )
    p = _provider(env)
    with pytest.raises(SpotifyAuthError, match="refresh_token"):
        await p.exchange_code(code="C", verifier="V")
    await p.aclose()
