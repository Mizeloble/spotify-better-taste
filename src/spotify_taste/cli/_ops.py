"""무엇/왜
운영 진단 명령. 알림 채널이 실제로 도착하는지, 수집기가 살아 있는지 확인한다.

설계 원칙
- `test-alert` 는 쿨다운을 타지 않는 종류(`AlertKind.TEST`)를 쓴다. 설정을
  확인하려는데 "30분 전에 보냈으니 안 보냄"이 나오면 진단이 안 된다.

어떻게 확인
    uv run spotify-taste ops test-alert
"""

from __future__ import annotations

import typer
from rich.console import Console

from ..config import get_settings
from ..notify import AlertKind, build_notifier

app = typer.Typer(help="운영 진단", no_args_is_help=True)
console = Console()


@app.command("test-alert")
def test_alert() -> None:
    """알림 채널이 살아 있는지 확인한다 (실제로 메시지가 간다)."""
    settings = get_settings()
    if not settings.has_telegram:
        console.print(
            "[yellow]알림 채널이 설정되지 않았습니다.[/yellow] "
            "`.env` 의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 채우세요."
        )
        raise typer.Exit(code=1)

    sent = build_notifier(settings).send(
        AlertKind.TEST,
        "알림 테스트",
        "이 메시지가 보이면 장애 알림 경로가 살아 있습니다.",
    )
    if not sent:
        console.print("[red]전송 실패[/red] — 로그의 notify.telegram_* 이벤트를 확인하세요.")
        raise typer.Exit(code=1)
    console.print("[green]전송 완료[/green] — Telegram 을 확인하세요.")
