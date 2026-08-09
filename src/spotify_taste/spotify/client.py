"""무엇/왜
Spotify Web API 로 나가는 **유일한** 출구. 인증 헤더, 레이트리밋, 재시도,
원본 아카이브가 전부 여기를 지난다.

설계 원칙
- **`idempotent` 플래그.** 조회는 5xx 에 재시도해도 안전하지만, 플레이리스트
  생성/추가는 아니다 — 이미 반영된 요청을 다시 보내면 곡이 두 번 들어간다.
  429 만은 상태 변경 요청에도 재시도한다(레이트리밋은 요청이 *처리되지 않았음*을
  뜻하므로).
- **401 은 딱 한 번만 재시도한다.** 토큰을 강제 갱신하고 다시 시도해서도 401 이면
  그건 만료가 아니라 scope 부족이나 계정 문제다 — 무한 갱신 루프를 돌면 안 된다.
- **204 는 오류가 아니다.** `/me/player` 는 아무것도 재생하지 않을 때 204 를
  준다. 그게 정상 상태이고, 오류로 다루면 백오프가 잘못 걸린다.
- **원본 아카이브는 요청 성공/실패와 무관하게 훅으로 뺀다.** 수집(저장)이
  해석(파싱)에 막히면 안 된다는 규칙의 HTTP 층 구현.

어떻게 확인
    uv run pytest tests/test_client_retry.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from ..config import Settings, get_settings
from .auth import TokenProvider
from .endpoints import API_BASE
from .ratelimit import RateLimiter, RetryAfterTooLong

# RetryAfterTooLong 는 호출부(백필/폴러)가 잡아야 하므로 여기서 재수출한다.
__all__ = ["RetryAfterTooLong", "SpotifyApiError", "SpotifyClient", "SpotifyResponse"]

log = structlog.get_logger(__name__)

# 재시도할 상태 코드. 429 는 별도 경로(Retry-After)로 처리한다.
RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_RETRIES = 3
BACKOFF_BASE_SEC = 0.5

REQUEST_TIMEOUT_SEC = 20.0


class SpotifyApiError(Exception):
    """API 가 오류를 돌려줬다."""

    def __init__(self, status: int, endpoint: str, detail: str = "") -> None:
        super().__init__(f"{endpoint} → HTTP {status}{f': {detail}' if detail else ''}")
        self.status = status
        self.endpoint = endpoint


@dataclass(frozen=True)
class SpotifyResponse:
    """파싱된 응답 + 아카이브에 필요한 원문.

    `body_text` 를 함께 들고 다니는 이유: 원본을 그대로 보관해야 나중에 필드가
    사라졌을 때 재구성할 수 있다. 파싱된 dict 만 남기면 그 순간의 우리 가정이
    섞여 들어간다.
    """

    status: int
    json: Any
    body_text: str | None
    endpoint: str
    params: dict[str, Any] | None

    @property
    def is_no_content(self) -> bool:
        """204. `/me/player` 에서는 '아무것도 재생 중이 아님'이라는 정상 상태."""
        return self.status == 204


class SpotifyClient:
    """인증·페이싱·재시도가 붙은 API 클라이언트."""

    def __init__(
        self,
        *,
        token_provider: TokenProvider,
        limiter: RateLimiter | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        archive: Callable[[SpotifyResponse, str], None] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = token_provider
        self._limiter = limiter or RateLimiter(
            max_retry_after_sec=float(self._settings.max_retry_after_sec)
        )
        self._client = client
        self._owns_client = client is None
        # (응답, source_job) → 저장. 실패해도 요청 흐름을 막지 않는다.
        self._archive = archive

    async def __aenter__(self) -> SpotifyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=API_BASE, timeout=REQUEST_TIMEOUT_SEC)
        return self._client

    @property
    def limiter(self) -> RateLimiter:
        return self._limiter

    # --- 핵심 ------------------------------------------------------------

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        path_params: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        role: str = "cli",
        source_job: str = "cli",
        idempotent: bool = True,
        acquire_timeout: float | None = None,
        ok_statuses: frozenset[int] = frozenset({200, 201, 204}),
    ) -> SpotifyResponse | None:
        """API 호출. 슬롯을 얻지 못하면 None (호출부가 틱을 건너뛴다).

        `endpoint` 는 `/tracks/{id}` 같은 **템플릿**을 그대로 넘긴다 — 아카이브
        키가 되어야 하므로 여기서 치환한다.
        """
        path = endpoint.format(**path_params) if path_params else endpoint

        got_slot = await self._limiter.acquire(role, timeout=acquire_timeout)
        if not got_slot:
            log.info("spotify.slot_timeout", endpoint=endpoint, role=role)
            return None

        attempt = 0
        refreshed_once = False
        while True:
            token = await self._tokens.get_access_token()
            try:
                resp = await self._http().request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                if idempotent and attempt < MAX_RETRIES:
                    await self._backoff(attempt, reason=type(exc).__name__, endpoint=endpoint)
                    attempt += 1
                    continue
                raise SpotifyApiError(0, endpoint, type(exc).__name__) from exc

            # 401: 토큰이 죽었을 수 있다. 딱 한 번 강제 갱신 후 재시도.
            if resp.status_code == 401 and not refreshed_once:
                refreshed_once = True
                log.info("spotify.unauthorized_retry", endpoint=endpoint)
                await self._force_refresh()
                continue

            if resp.status_code == 429:
                wait = self._limiter.note_rate_limited(_retry_after(resp))
                # 429 는 '처리되지 않음'이므로 상태 변경 요청도 재시도해도 안전하다.
                if attempt < MAX_RETRIES:
                    attempt += 1
                    # 실제 대기는 다음 acquire() 가 락 밖에서 한다.
                    if not await self._limiter.acquire(role, timeout=acquire_timeout):
                        log.info("spotify.slot_timeout_after_429", endpoint=endpoint)
                        return None
                    continue
                raise SpotifyApiError(429, endpoint, f"retry_after={wait:.0f}s")

            if resp.status_code in RETRY_STATUSES and idempotent and attempt < MAX_RETRIES:
                await self._backoff(attempt, reason=str(resp.status_code), endpoint=endpoint)
                attempt += 1
                continue

            self._limiter.note_success()
            parsed = self._to_response(resp, endpoint, params)
            self._archive_safely(parsed, source_job)

            if resp.status_code not in ok_statuses:
                raise SpotifyApiError(resp.status_code, endpoint, _error_message(resp))
            return parsed

    async def get(self, endpoint: str, **kwargs: Any) -> SpotifyResponse | None:
        return await self.request("GET", endpoint, **kwargs)

    async def post(self, endpoint: str, **kwargs: Any) -> SpotifyResponse | None:
        # 상태 변경은 기본적으로 재시도하지 않는다.
        kwargs.setdefault("idempotent", False)
        return await self.request("POST", endpoint, **kwargs)

    async def paginate(
        self,
        endpoint: str,
        *,
        limit: int = 50,
        max_pages: int = 100,
        items_key: str = "items",
        **kwargs: Any,
    ) -> list[dict]:
        """offset 기반 페이징을 끝까지 따라가 항목을 모은다.

        `max_pages` 는 안전장치다. 응답이 이상해서 `next` 가 계속 나오면
        무한 루프가 아니라 잘린 결과와 경고 로그가 되게 한다.
        """
        params = dict(kwargs.pop("params", None) or {})
        params.setdefault("limit", limit)
        collected: list[dict] = []
        offset = 0
        for page in range(max_pages):
            params["offset"] = offset
            resp = await self.get(endpoint, params=dict(params), **kwargs)
            if resp is None or resp.json is None:
                break
            body = resp.json
            items = body.get(items_key) or []
            collected.extend(items)
            if not body.get("next") or not items:
                break
            offset += len(items)
            if page == max_pages - 1:
                log.warning("spotify.pagination_truncated", endpoint=endpoint, pages=max_pages)
        return collected

    # --- 보조 ------------------------------------------------------------

    async def _force_refresh(self) -> None:
        """액세스 토큰을 만료된 것으로 취급해 강제 갱신한다.

        갱신 실패의 알림·센티넬 처리는 TokenProvider 의 몫이라 여기선 재전파만
        한다.
        """
        await self._tokens.force_refresh()

    async def _backoff(self, attempt: int, *, reason: str, endpoint: str) -> None:
        delay = BACKOFF_BASE_SEC * (2**attempt)
        log.warning(
            "spotify.retry", endpoint=endpoint, reason=reason, attempt=attempt + 1, delay=delay
        )
        await asyncio.sleep(delay)

    def _to_response(
        self, resp: httpx.Response, endpoint: str, params: dict[str, Any] | None
    ) -> SpotifyResponse:
        if resp.status_code == 204 or not resp.content:
            return SpotifyResponse(resp.status_code, None, None, endpoint, params)
        text = resp.text
        try:
            parsed = resp.json()
        except ValueError:
            # 파싱 실패해도 원문은 살린다 — 나중에 무슨 일이 있었는지 봐야 한다.
            log.warning("spotify.invalid_json", endpoint=endpoint, status=resp.status_code)
            parsed = None
        return SpotifyResponse(resp.status_code, parsed, text, endpoint, params)

    def _archive_safely(self, resp: SpotifyResponse, source_job: str) -> None:
        if self._archive is None:
            return
        try:
            self._archive(resp, source_job)
        except Exception as exc:
            # 아카이브 실패가 수집을 멈출 수 없다.
            log.warning("spotify.archive_failed", endpoint=resp.endpoint, error=type(exc).__name__)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # HTTP-date 형식일 수도 있다. 파싱하지 않고 보수적으로 기본값에 맡긴다.
        log.warning("spotify.retry_after_unparsable", value=raw)
        return None


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message", ""))
        return str(error or "")
    return ""
