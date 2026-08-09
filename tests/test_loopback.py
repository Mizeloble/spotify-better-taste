"""루프백 콜백 서버 — 실제 소켓으로 구동한다.

mock 이 아니라 진짜 TCP 를 쓰는 이유: 여기서 잡고 싶은 버그(favicon 요청을
콜백으로 오인, 브라우저가 먼저 끊음)는 전부 소켓 수준에서만 재현된다.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from spotify_taste.spotify.loopback import CallbackTimeout, catch_callback


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _request(port: int, target: str) -> str:
    """요청 한 줄을 보내고 응답 전체를 읽는다."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    body = await reader.read()
    writer.close()
    return body.decode("utf-8", errors="replace")


async def _serve(port: int, **kwargs):
    """서버를 띄우고, 리스닝이 시작된 뒤에 제어를 돌려준다."""
    ready = asyncio.Event()
    task = asyncio.create_task(catch_callback(port=port, ready=ready, **kwargs))
    await ready.wait()
    return task


async def test_extracts_code_and_state() -> None:
    port = _free_port()
    task = await _serve(port)
    await _request(port, "/callback?code=AUTH_CODE&state=ST")
    result = await task
    assert result.ok
    assert (result.code, result.state, result.error) == ("AUTH_CODE", "ST", None)


async def test_success_page_is_html() -> None:
    port = _free_port()
    task = await _serve(port)
    resp = await _request(port, "/callback?code=c&state=s")
    await task
    assert "200 OK" in resp
    assert "text/html" in resp
    assert "인증이 완료" in resp


async def test_favicon_request_does_not_end_the_wait() -> None:
    """브라우저가 /favicon.ico 를 먼저 요청하는 일이 흔하다. 그걸 콜백으로
    오인하면 인증이 매번 실패한다 — 이 프로젝트에서 경로를 검사하는 이유."""
    port = _free_port()
    task = await _serve(port)

    resp = await _request(port, "/favicon.ico")
    assert "404" in resp
    assert not task.done()

    await _request(port, "/callback?code=REAL&state=s")
    result = await task
    assert result.code == "REAL"


async def test_root_request_ignored_too() -> None:
    port = _free_port()
    task = await _serve(port)
    await _request(port, "/")
    assert not task.done()
    await _request(port, "/callback?code=c&state=s")
    assert (await task).code == "c"


async def test_user_denial_returned_as_value_not_exception() -> None:
    """CLI 가 '사용자가 거부했습니다'를 제대로 찍으려면 값으로 받아야 한다."""
    port = _free_port()
    task = await _serve(port)
    await _request(port, "/callback?error=access_denied&state=s")
    result = await task
    assert not result.ok
    assert result.error == "access_denied"
    assert result.code is None


async def test_error_page_shown_on_denial() -> None:
    port = _free_port()
    task = await _serve(port)
    resp = await _request(port, "/callback?error=access_denied")
    await task
    assert "인증이 취소" in resp


async def test_timeout_raises_with_actionable_message() -> None:
    port = _free_port()
    with pytest.raises(CallbackTimeout) as exc:
        await catch_callback(port=port, timeout_sec=0.05)
    # 사람이 뭘 고쳐야 하는지가 메시지에 있어야 한다.
    assert "redirect URI" in str(exc.value)
    assert f"127.0.0.1:{port}/callback" in str(exc.value)


async def test_port_is_released_after_completion() -> None:
    """서버가 포트를 잡고 있으면 재인증이 'address in use' 로 실패한다."""
    port = _free_port()
    task = await _serve(port)
    await _request(port, "/callback?code=c&state=s")
    await task

    task2 = await _serve(port)
    await _request(port, "/callback?code=c2&state=s")
    assert (await task2).code == "c2"


async def test_connection_that_sends_nothing_is_tolerated() -> None:
    """브라우저의 preconnect 는 아무것도 보내지 않고 끊는다."""
    port = _free_port()
    task = await _serve(port)

    _, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await asyncio.sleep(0)
    assert not task.done()

    await _request(port, "/callback?code=c&state=s")
    assert (await task).code == "c"


def _outbound_ip() -> str | None:
    """이 머신의 비-루프백 IPv4. 없으면 None.

    `gethostbyname(gethostname())` 은 macOS 에서 127.0.0.1 을 돌려주는 일이
    많아 쓸 수 없다. UDP 소켓을 connect 하면 패킷을 보내지 않고도 커널이
    고른 출구 인터페이스 주소를 알 수 있다.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


async def test_binds_only_to_loopback() -> None:
    """0.0.0.0 에 바인딩하면 같은 네트워크의 누구나 인증 코드를 가로챈다."""
    local_ip = _outbound_ip()
    if local_ip is None:
        pytest.skip("비-루프백 주소가 없는 환경 (오프라인)")

    port = _free_port()
    task = await _serve(port)
    try:
        with pytest.raises((ConnectionRefusedError, OSError, TimeoutError)):
            await asyncio.wait_for(asyncio.open_connection(local_ip, port), timeout=2.0)
    finally:
        await _request(port, "/callback?code=c&state=s")
        await task


async def test_blank_error_value_still_counts_as_error() -> None:
    port = _free_port()
    task = await _serve(port)
    await _request(port, "/callback?error=")
    result = await task
    assert result.error == ""
    assert not result.ok
