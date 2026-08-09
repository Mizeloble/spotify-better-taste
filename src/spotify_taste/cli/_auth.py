"""무엇/왜
인증 명령. 브라우저를 띄워 승인을 받고 토큰 번들을 저장한다.

설계 원칙
- `login` 은 브라우저를 열고 **URL 도 함께 출력한다.** SSH 나 헤드리스에서
  webbrowser.open() 이 조용히 실패하면 사람이 붙여 넣을 수 있어야 한다.
- state 불일치는 **토큰 교환 전에** 중단한다. 교환하고 나서 검사하면 늦다.
- `status` 는 네트워크를 쓰지 않는다. 로컬 번들이 어떤 상태인지만 본다.
  계정이 Premium 인지 같은 실시간 확인은 `ops preflight` 의 몫이다 — 진단
  명령이 네트워크 장애로 실패하면 정작 알고 싶은 로컬 상태도 못 본다.

어떻게 확인
    uv run spotify-taste auth login
    uv run spotify-taste auth status
"""

from __future__ import annotations

import asyncio
import webbrowser

import typer
from rich.console import Console

from .._common import epoch_ms_to_kst, now_ms
from ..config import get_settings
from ..notify import build_notifier
from ..spotify.auth import TokenProvider
from ..spotify.loopback import CallbackTimeout, catch_callback
from ..spotify.pkce import authorize_url, new_challenge, state_matches
from ..spotify.token_store import TokenStore

app = typer.Typer(help="Spotify 인증 (Authorization Code with PKCE)", no_args_is_help=True)
console = Console()


@app.command("login")
def login(
    timeout: float = typer.Option(300.0, help="리다이렉트 대기 시간(초)"),
    no_browser: bool = typer.Option(False, "--no-browser", help="브라우저를 열지 않는다"),
) -> None:
    """브라우저로 승인을 받고 토큰을 저장한다."""
    settings = get_settings()
    if not settings.spotify_client_id:
        console.print(
            "[red]SPOTIFY_CLIENT_ID 가 비어 있습니다.[/red]\n"
            "developer.spotify.com/dashboard 에서 앱을 만들고 `.env` 에 채우세요. "
            "Client Secret 은 필요 없습니다 (PKCE)."
        )
        raise typer.Exit(code=1)

    host, port, path = _parse_redirect(settings.spotify_redirect_uri)
    challenge = new_challenge()
    url = authorize_url(
        client_id=settings.spotify_client_id,
        redirect_uri=settings.spotify_redirect_uri,
        scopes=settings.scopes,
        challenge=challenge,
    )

    console.print("브라우저에서 승인하세요. 열리지 않으면 아래 주소를 붙여 넣으세요:\n")
    console.print(f"[cyan]{url}[/cyan]\n")
    if not no_browser:
        webbrowser.open(url)

    try:
        result = asyncio.run(
            catch_callback(host=host, port=port, callback_path=path, timeout_sec=timeout)
        )
    except CallbackTimeout as exc:
        console.print(f"[red]시간 초과[/red]\n{exc}")
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        console.print(
            f"[red]{host}:{port} 를 열 수 없습니다:[/red] {exc}\n"
            "다른 프로세스가 포트를 쓰고 있는지 확인하세요."
        )
        raise typer.Exit(code=1) from exc

    if result.error is not None:
        console.print(f"[red]승인이 거부되었습니다:[/red] {result.error or '(사유 없음)'}")
        raise typer.Exit(code=1)

    # CSRF 방어. 교환 전에 막아야 의미가 있다.
    if not state_matches(challenge.state, result.state):
        console.print(
            "[red]state 불일치[/red] — 요청과 응답이 짝이 맞지 않습니다. "
            "토큰을 교환하지 않고 중단합니다. 다시 시도하세요."
        )
        raise typer.Exit(code=1)

    assert result.code is not None
    bundle = asyncio.run(_exchange(result.code, challenge.verifier))

    console.print("[green]인증 완료[/green]")
    console.print(f"  저장 위치 : {settings.spotify_token_path or '(메모리 전용)'}")
    console.print(f"  만료      : {epoch_ms_to_kst(bundle.expires_at_ms):%Y-%m-%d %H:%M:%S} KST")
    _print_scopes(bundle.scopes)
    console.print(
        "\n다음: [cyan]uv run spotify-taste ops preflight[/cyan] 로 계정 상태를 확인하세요."
    )


async def _exchange(code: str, verifier: str):
    settings = get_settings()
    provider = TokenProvider(settings=settings, notifier=build_notifier(settings))
    try:
        return await provider.exchange_code(code=code, verifier=verifier)
    finally:
        await provider.aclose()


@app.command("status")
def status() -> None:
    """저장된 토큰의 상태를 본다 (네트워크를 쓰지 않는다)."""
    settings = get_settings()
    store = TokenStore(path=settings.spotify_token_path)
    bundle = store.load(expected_client_id=settings.spotify_client_id)

    console.print(f"Client ID : {settings.spotify_client_id or '[red](미설정)[/red]'}")
    console.print(f"토큰 경로 : {settings.spotify_token_path or '(메모리 전용)'}")

    if settings.reauth_sentinel_path.exists():
        console.print(
            "\n[red]재인증 필요[/red] — refresh token 이 폐기되었습니다.\n"
            "  [cyan]uv run spotify-taste auth login[/cyan]"
        )
        raise typer.Exit(code=1)

    if bundle is None:
        console.print("\n[yellow]저장된 토큰이 없습니다.[/yellow] `auth login` 을 실행하세요.")
        raise typer.Exit(code=1)

    remaining_min = (bundle.expires_at_ms - now_ms()) / 60_000
    expiry_style = "green" if remaining_min > 5 else "yellow"
    console.print(
        f"만료      : [{expiry_style}]"
        f"{epoch_ms_to_kst(bundle.expires_at_ms):%Y-%m-%d %H:%M:%S} KST "
        f"({remaining_min:+.0f}분)[/{expiry_style}]"
    )
    console.print(
        "  (만료는 정상입니다 — 폴러가 자동 갱신합니다. refresh token 이 살아 있으면 됩니다.)"
    )
    _print_scopes(bundle.scopes)


@app.command("logout")
def logout(
    yes: bool = typer.Option(False, "--yes", "-y", help="확인 없이 진행"),
) -> None:
    """저장된 토큰을 지운다. 다시 쓰려면 `auth login` 이 필요하다."""
    settings = get_settings()
    if not yes:
        typer.confirm("저장된 토큰을 삭제합니다. 계속할까요?", abort=True)
    TokenStore(path=settings.spotify_token_path).invalidate()
    console.print("[green]삭제했습니다.[/green]")


def _print_scopes(granted: tuple[str, ...]) -> None:
    """부여된 scope 와 필요한 scope 를 비교해 보여 준다.

    scope 가 모자라면 특정 엔드포인트만 403 이 나서 원인을 찾기 어렵다.
    """
    settings = get_settings()
    granted_set = set(granted)
    missing = [s for s in settings.scopes if s not in granted_set]
    console.print(f"scope     : {len(granted_set)}/{len(settings.scopes)}")
    if missing:
        console.print("  [yellow]누락:[/yellow] " + ", ".join(missing))
        console.print("  → `auth login` 을 다시 실행해 승인하세요.")


def _parse_redirect(uri: str) -> tuple[str, int, str]:
    """redirect URI 에서 (host, port, path) 를 뽑는다.

    루프백 서버를 어디에 띄울지는 등록된 URI 가 결정한다 — 둘이 어긋나면
    Spotify 가 리다이렉트를 거부한다.
    """
    from urllib.parse import urlparse

    parsed = urlparse(uri)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8888
    return host, port, parsed.path or "/callback"
