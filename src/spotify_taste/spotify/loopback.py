"""무엇/왜
OAuth 리다이렉트를 받아 내는 일회용 로컬 HTTP 서버. 브라우저가
`http://127.0.0.1:8888/callback?code=…&state=…` 로 오면 쿼리를 읽고 종료한다.

설계 원칙
- `asyncio.start_server` 40여 줄. `http.server`(동기·스레드라 비동기 CLI 에서
  취소/타임아웃이 어색)도, aiohttp/fastapi(쿼리스트링 하나 읽자고 웹 프레임워크)도
  아니다.
- **`/callback` 이 아닌 요청은 404 로 답하고 계속 기다린다.** 브라우저가
  `/favicon.ico` 를 먼저 요청하는 일이 흔한데, 그걸 콜백으로 오인해 종료하면
  인증이 매번 실패한다.
- `?error=access_denied` 도 정상 반환한다. CLI 가 "사용자가 거부했습니다"를
  제대로 찍으려면 예외가 아니라 값으로 받아야 한다.
- 127.0.0.1 에만 바인딩한다. 0.0.0.0 이면 같은 네트워크의 누구나 인증 코드를
  가로챌 수 있다.

어떻게 확인
    uv run pytest tests/test_loopback.py
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlparse

import structlog

__all__ = ["CallbackResult", "CallbackTimeout", "catch_callback"]

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_SEC = 300.0
# 요청 라인만 읽으면 되므로 헤더 전체를 기다리지 않는다.
MAX_REQUEST_LINE_BYTES = 8_192

_SUCCESS_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>인증 완료</title></head><body style="font-family:system-ui;padding:3rem">
<h2>인증이 완료되었습니다.</h2><p>이 탭을 닫고 터미널로 돌아가세요.</p>
</body></html>"""

_ERROR_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>인증 실패</title></head><body style="font-family:system-ui;padding:3rem">
<h2>인증이 취소되었거나 실패했습니다.</h2><p>터미널의 안내를 확인하세요.</p>
</body></html>"""


class CallbackTimeout(Exception):
    """제한 시간 안에 리다이렉트가 오지 않았다."""


@dataclass(frozen=True)
class CallbackResult:
    """콜백으로 받은 값. `code` 와 `error` 중 하나만 채워진다."""

    code: str | None
    state: str | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.code is not None and self.error is None


async def catch_callback(
    *,
    host: str = "127.0.0.1",
    port: int = 8888,
    callback_path: str = "/callback",
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ready: asyncio.Event | None = None,
) -> CallbackResult:
    """콜백 하나를 받을 때까지 기다렸다가 결과를 돌려준다.

    `ready` 를 주면 리스닝이 시작된 직후에 set 된다 — 테스트가 "언제 접속해도
    되는지" 를 알 수 있게 하는 용도(sleep 기반 경합 회피).
    """
    loop = asyncio.get_running_loop()
    result: asyncio.Future[CallbackResult] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            request_line = raw.decode("latin-1", errors="replace").strip()
            target = request_line.split(" ")[1] if len(request_line.split(" ")) >= 2 else "/"
            parsed = urlparse(target)

            if parsed.path != callback_path:
                # favicon 등 부수 요청. 콜백으로 오인하지 않고 계속 기다린다.
                log.debug("auth.loopback_ignored_path", path=parsed.path)
                await _respond(writer, 404, "not found")
                return

            params = _parse_query(parsed.query)
            parsed_result = CallbackResult(
                code=params.get("code"),
                state=params.get("state"),
                error=params.get("error"),
            )
            await _respond(
                writer,
                200,
                _SUCCESS_HTML if parsed_result.ok else _ERROR_HTML,
                content_type="text/html; charset=utf-8",
            )
            if not result.done():
                result.set_result(parsed_result)
        except Exception as exc:
            # 연결 하나가 깨져도 서버는 계속 기다린다 — 브라우저의 사전 연결
            # (preconnect)이 아무것도 안 보내고 끊는 일이 흔하다.
            log.debug("auth.loopback_conn_error", error=type(exc).__name__, detail=str(exc))
        finally:
            writer.close()
            # 브라우저가 먼저 끊었으면 close 완료를 기다리다 예외가 난다.
            with contextlib.suppress(ConnectionResetError, BrokenPipeError):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, host=host, port=port)
    log.info("auth.loopback_listening", host=host, port=port, path=callback_path)
    if ready is not None:
        ready.set()
    try:
        async with server:
            return await asyncio.wait_for(result, timeout=timeout_sec)
    except TimeoutError as exc:
        raise CallbackTimeout(
            f"{timeout_sec:.0f}초 안에 리다이렉트가 오지 않았습니다. "
            f"브라우저에서 승인을 마쳤는지, redirect URI 가 "
            f"http://{host}:{port}{callback_path} 로 등록돼 있는지 확인하세요."
        ) from exc


def _parse_query(query: str) -> dict[str, str]:
    """쿼리스트링을 푼다.

    `parse_qs` 대신 `parse_qsl` 을 쓰는 이유는 값이 리스트로 감싸이지 않고,
    `keep_blank_values` 로 빈 값(`?error=`)을 None 이 아니라 빈 문자열로
    남길 수 있어서다 — "error 키가 있다"와 "없다"는 다른 상태다.
    """
    return dict(parse_qsl(query, keep_blank_values=True))


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    body: str,
    content_type: str = "text/plain; charset=utf-8",
) -> None:
    payload = body.encode("utf-8")
    reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
    head = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(head.encode("ascii") + payload)
    # 브라우저가 먼저 끊는 경우가 있다. 결과는 이미 파싱했으므로 무해하다.
    with contextlib.suppress(ConnectionResetError, BrokenPipeError):
        await writer.drain()
