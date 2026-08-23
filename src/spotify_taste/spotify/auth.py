"""무엇/왜
액세스 토큰의 수명 관리. 초기 코드 교환, 자동 갱신, 회전된 refresh token 의
영속화, 그리고 폐기됐을 때의 알림 경로.

설계 원칙
- **`accounts.spotify.com/api/token` 을 만지는 곳은 이 파일 하나뿐이다.**
  두 곳에서 갱신하면 회전된 refresh token 을 서로 덮어써 `invalid_grant` 로
  새벽에 수집이 죽는다. 그래서 폴러도 백필도 `SpotifyClient` 를 통해 여기로만
  들어온다.
- **회전된 refresh token 은 받는 즉시 저장한다.** 응답에 `refresh_token` 이
  들어 있으면 옛 것은 곧 무효가 된다 — 저장 전에 프로세스가 죽으면 재인증이다.
- **`invalid_grant` 는 재시도하지 않는다.** 사람이 브라우저 앞에 앉아야만
  풀리는 상태이므로, 재시도는 레이트리밋만 소모하고 알림만 늦춘다.
- **일시적 실패도 여기서 재시도하지 않는다.** 재시도 간격은 호출부인 폴러가
  이미 갖고 있고(`ERROR_BACKOFF_SEC`), 여기서 또 자면 두 폴러가 공유하는
  `_lock` 을 잡은 채 멈춰 종료 신호까지 늦어진다. 여기는 올리기만 하고,
  폴러가 `SpotifyAuthError` 를 전송 오류와 같이 취급해 공백을 남기고 물러난다.
- **갱신 실패에는 알람이 두 개 걸린다.** 여기서 보내는 Telegram 하나,
  그리고 정상종료 센티넬을 쓰지 않음으로써 heartbeat-check 가 독립적으로
  올리는 것 하나. 하나의 메커니즘만 믿지 않는다.

어떻게 확인
    uv run spotify-taste auth login
    uv run pytest tests/test_auth_refresh.py
"""

from __future__ import annotations

import asyncio

import httpx
import structlog

from .._common import now_ms
from ..clock import Clock, SystemClock
from ..config import Settings, get_settings
from ..notify import AlertKind, NoopNotifier, Notifier
from .token_store import TokenBundle, TokenStore

__all__ = [
    "TOKEN_URL",
    "SpotifyAuthError",
    "SpotifyAuthRevoked",
    "TokenProvider",
]

log = structlog.get_logger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"

# 이만큼 연속 실패하면 Telegram 을 울린다. 폴러의 오류 백오프(5·10·20·40·60초)를
# 타고 오므로 10회는 약 6분이다. 이 카운터는 프로세스가 살아 있어야만 의미가
# 있다 — 2026-08-22 에는 갱신 실패가 프로세스를 죽여 매번 1 에서 리셋됐고,
# 그래서 9시간 20분 동안 알림이 한 번도 오르지 않았다.
ALERT_AFTER_CONSECUTIVE_FAILURES = 10
LOG_AFTER_CONSECUTIVE_FAILURES = 3

TOKEN_REQUEST_TIMEOUT_SEC = 15.0


class SpotifyAuthError(Exception):
    """갱신에 실패했지만 회복 가능성이 있는 상태 (네트워크, 5xx)."""


class SpotifyAuthRevoked(SpotifyAuthError):
    """refresh token 이 영구 폐기됐다. 사람이 재인증해야 한다."""


class TokenProvider:
    """유효한 액세스 토큰을 공급한다. 갱신은 여기서만 일어난다."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        store: TokenStore | None = None,
        client: httpx.AsyncClient | None = None,
        notifier: Notifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._store = store or TokenStore(path=self._settings.spotify_token_path)
        self._client = client
        self._owns_client = client is None
        self._notifier = notifier or NoopNotifier()
        self._clock = clock or SystemClock()

        self._bundle: TokenBundle | None = None
        # 동시 호출자가 여럿이어도 갱신은 한 번만 일어나야 한다.
        self._lock = asyncio.Lock()
        self._consecutive_failures = 0

    # --- 수명주기 ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TOKEN_REQUEST_TIMEOUT_SEC)
        return self._client

    # --- 조회 -------------------------------------------------------------

    @property
    def bundle(self) -> TokenBundle | None:
        """현재 번들 (없으면 디스크에서 읽어 본다)."""
        if self._bundle is None:
            self._bundle = self._store.load(expected_client_id=self._settings.spotify_client_id)
        return self._bundle

    async def get_access_token(self) -> str:
        """유효한 액세스 토큰. 필요하면 갱신한다.

        `SpotifyClient.request()` 만 이 메서드를 부른다.
        """
        async with self._lock:
            bundle = self.bundle
            if bundle is None:
                raise SpotifyAuthRevoked(
                    "저장된 토큰이 없습니다. `uv run spotify-taste auth login` 을 실행하세요."
                )
            if not bundle.is_expired(now=self._clock.now_ms()):
                return bundle.access_token
            refreshed = await self._refresh(bundle)
            return refreshed.access_token

    async def force_refresh(self) -> str:
        """캐시된 액세스 토큰을 버리고 새로 받는다.

        `SpotifyClient` 가 401 을 한 번 만났을 때 쓴다 — 만료 시각은 멀쩡한데
        서버가 거부하는 경우(토큰 폐기, 시계 어긋남)가 실제로 있다.
        """
        async with self._lock:
            bundle = self.bundle
            if bundle is None:
                raise SpotifyAuthRevoked(
                    "저장된 토큰이 없습니다. `uv run spotify-taste auth login` 을 실행하세요."
                )
            refreshed = await self._refresh(bundle)
            return refreshed.access_token

    # --- 초기 교환 --------------------------------------------------------

    async def exchange_code(self, *, code: str, verifier: str) -> TokenBundle:
        """authorization code 를 토큰 번들로 바꾸고 저장한다."""
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.spotify_redirect_uri,
            "client_id": self._settings.spotify_client_id,
            "code_verifier": verifier,
        }
        data = await self._post_token(payload)
        bundle = self._bundle_from_response(data, previous=None)
        self._store.save(bundle)
        self._bundle = bundle
        self._clear_reauth_sentinel()
        self._consecutive_failures = 0
        return bundle

    # --- 갱신 -------------------------------------------------------------

    async def _refresh(self, previous: TokenBundle) -> TokenBundle:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": previous.refresh_token,
            "client_id": self._settings.spotify_client_id,
        }
        data = await self._post_token(payload)
        bundle = self._bundle_from_response(data, previous=previous)
        # 회전된 refresh token 은 받는 즉시 저장한다. 저장 전에 죽으면 재인증.
        self._store.save(bundle)
        self._bundle = bundle
        self._consecutive_failures = 0
        rotated = bundle.refresh_token != previous.refresh_token
        log.info("auth.refreshed", rotated=rotated, expires_at_ms=bundle.expires_at_ms)
        return bundle

    async def _post_token(self, payload: dict[str, str]) -> dict:
        """토큰 엔드포인트 호출. PKCE 라 Authorization 헤더도 시크릿도 없다."""
        try:
            resp = await self._http().post(
                TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            self._on_transient_failure(type(exc).__name__)
            raise SpotifyAuthError(f"토큰 요청 실패: {type(exc).__name__}") from exc

        if resp.status_code == 400:
            error = _error_code(resp)
            if error == "invalid_grant":
                # 종료 사유. 재시도는 레이트리밋만 소모하고 알림만 늦춘다.
                self._on_revoked()
                raise SpotifyAuthRevoked(
                    "refresh token 이 폐기되었습니다. "
                    "`uv run spotify-taste auth login` 으로 재인증하세요."
                )
            self._on_transient_failure(f"400:{error}")
            raise SpotifyAuthError(f"토큰 요청 거부: {error}")

        if resp.status_code >= 400:
            self._on_transient_failure(f"http:{resp.status_code}")
            raise SpotifyAuthError(f"토큰 요청 실패: HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            self._on_transient_failure("invalid_json")
            raise SpotifyAuthError("토큰 응답을 파싱할 수 없습니다") from exc
        return dict(data)

    def _bundle_from_response(self, data: dict, *, previous: TokenBundle | None) -> TokenBundle:
        now = self._clock.now_ms()
        expires_in = int(data.get("expires_in", 3600))
        # 갱신 응답은 refresh_token 을 생략할 수 있다 — 그러면 기존 것을 계속 쓴다.
        refresh = str(data.get("refresh_token") or (previous.refresh_token if previous else ""))
        if not refresh:
            raise SpotifyAuthError("응답에 refresh_token 이 없습니다")
        scope_text = data.get("scope") or ""
        scopes = tuple(scope_text.split()) if scope_text else (previous.scopes if previous else ())
        return TokenBundle(
            client_id=self._settings.spotify_client_id,
            access_token=str(data["access_token"]),
            refresh_token=refresh,
            expires_at_ms=now + expires_in * 1000,
            scopes=scopes,
            obtained_at_ms=now,
        )

    # --- 실패 처리 --------------------------------------------------------

    def _on_transient_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        n = self._consecutive_failures
        if n >= ALERT_AFTER_CONSECUTIVE_FAILURES:
            self._notifier.send(
                AlertKind.AUTH_REFRESH_FAILING,
                "토큰 갱신이 계속 실패합니다",
                f"연속 {n}회 실패 (마지막 원인: {reason}).\n"
                "네트워크나 Spotify 장애일 수 있습니다. 수집기는 계속 돌며 공백을 기록합니다.",
            )
        elif n >= LOG_AFTER_CONSECUTIVE_FAILURES:
            log.warning("auth.refresh_failing", consecutive=n, reason=reason)
        else:
            log.info("auth.refresh_retry", consecutive=n, reason=reason)

    def _on_revoked(self) -> None:
        """토큰이 영구 폐기됐다. 알림 + 센티넬 + 캐시 무효화."""
        log.error("auth.revoked")
        self._notifier.send(
            AlertKind.AUTH_REVOKED,
            "재인증이 필요합니다",
            "refresh token 이 폐기되어 수집이 멈췄습니다.\n"
            "다음을 실행하세요:\n"
            "  cd ~/Project/spotify-better-taste && uv run spotify-taste auth login",
        )
        self._write_reauth_sentinel()
        # 폐기된 번들을 계속 들고 있으면 재시작 때마다 같은 실패를 반복한다.
        self._store.invalidate()
        self._bundle = None

    def _write_reauth_sentinel(self) -> None:
        """launchd 래퍼가 보고 exit 0 으로 빠져나갈 표식.

        이게 없으면 KeepAlive 가 죽은 토큰으로 재시작 폭풍을 일으킨다.
        """
        path = self._settings.reauth_sentinel_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"refresh token revoked at {now_ms()}\nrun: uv run spotify-taste auth login\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("auth.sentinel_write_failed", error=str(exc))

    def _clear_reauth_sentinel(self) -> None:
        try:
            self._settings.reauth_sentinel_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("auth.sentinel_clear_failed", error=str(exc))


def _error_code(resp: httpx.Response) -> str:
    """토큰 엔드포인트의 오류 코드. 파싱 실패는 'unknown'."""
    try:
        body = resp.json()
    except ValueError:
        return "unknown"
    if isinstance(body, dict):
        return str(body.get("error") or "unknown")
    return "unknown"
