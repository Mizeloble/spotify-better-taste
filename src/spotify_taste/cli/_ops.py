"""무엇/왜
운영 진단 명령. 알림 채널이 실제로 도착하는지, 수집기가 살아 있는지 확인한다.

설계 원칙
- `test-alert` 는 쿨다운을 타지 않는 종류(`AlertKind.TEST`)를 쓴다. 설정을
  확인하려는데 "30분 전에 보냈으니 안 보냄"이 나오면 진단이 안 된다.

어떻게 확인
    uv run spotify-taste ops test-alert
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from .._common import epoch_ms_to_kst, now_ms
from ..config import get_settings
from ..db.connection import connect
from ..db.queries import SELECT_LAST_SAMPLE_AT
from ..heartbeat import DEFAULT_MAX_AGE_SEC, HeartbeatStatus, check_heartbeat
from ..notify import AlertKind, build_notifier
from ..spotify.token_store import TokenStore

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


@app.command("heartbeat-check")
def heartbeat_check(
    max_age_sec: int = typer.Option(DEFAULT_MAX_AGE_SEC, help="이보다 오래되면 장애로 본다"),
    quiet: bool = typer.Option(False, "--quiet", help="정상이면 아무것도 출력하지 않는다"),
) -> None:
    """수집기가 살아 있는지 확인하고, 아니면 알린다 (5분 주기 launchd 잡).

    **파일 mtime 만 보지 않는다.** 토큰이 죽었거나 DB 락에 걸린 프로세스도
    루프는 계속 돌기 때문에, 실제로 적재된 마지막 샘플 시각을 함께 본다.
    """
    settings = get_settings()

    last_sample_at: int | None = None
    if settings.db_path.exists():
        conn = connect(settings.db_path, readonly=True)
        try:
            row = conn.execute(SELECT_LAST_SAMPLE_AT).fetchone()
            last_sample_at = int(row[0]) if row and row[0] is not None else None
        finally:
            conn.close()

    verdict = check_heartbeat(
        path=settings.heartbeat_path,
        now_ms_value=now_ms(),
        max_age_sec=max_age_sec,
        stopped_sentinel=settings.shutdown_sentinel_path,
        last_sample_at=last_sample_at,
    )

    if verdict.status is HeartbeatStatus.OK:
        if not quiet:
            console.print(f"[green]정상[/green] ({verdict.age_sec:.0f}초 전)")
        return
    if verdict.status is HeartbeatStatus.STOPPED:
        if not quiet:
            console.print("[dim]정상 종료 상태[/dim] — 알리지 않습니다.")
        return

    console.print(f"[red]{verdict.status}[/red] — {verdict.detail}")
    body = verdict.detail
    if settings.reauth_sentinel_path.exists():
        body += "\n\n재인증이 필요합니다:\n  uv run spotify-taste auth login"
    build_notifier(settings).send(AlertKind.COLLECTOR_STALE, "수집기가 멈춘 것 같습니다", body)
    raise typer.Exit(code=1)


@app.command("preflight")
def preflight() -> None:
    """운영을 시작하기 전 전제 조건을 전부 확인한다.

    `auth status` 와 달리 **네트워크를 쓴다** — 계정이 Premium 인지는
    실제로 물어봐야 알 수 있다.
    """
    settings = get_settings()
    problems: list[str] = []

    # 1. 설정
    if not settings.spotify_client_id:
        problems.append("SPOTIFY_CLIENT_ID 가 비어 있습니다 (.env)")
    if not settings.has_telegram:
        console.print("[yellow]경고[/yellow] 알림 채널이 없습니다 — 장애를 조용히 놓칩니다.")

    # 2. DB
    if not settings.db_path.exists():
        problems.append(f"DB 가 없습니다: `db migrate` 를 실행하세요 ({settings.db_path})")
    else:
        console.print(f"[green]OK[/green]  DB          {settings.db_path}")

    # 3. 토큰
    bundle = TokenStore(path=settings.spotify_token_path).load(
        expected_client_id=settings.spotify_client_id
    )
    if bundle is None:
        problems.append("저장된 토큰이 없습니다: `auth login` 을 실행하세요")
    else:
        missing = [s for s in settings.scopes if s not in set(bundle.scopes)]
        if missing:
            problems.append(f"scope 누락: {', '.join(missing)} — `auth login` 을 다시 실행하세요")
        else:
            console.print(
                f"[green]OK[/green]  scope       {len(bundle.scopes)}/{len(settings.scopes)}"
            )

    if problems:
        _report(problems)
        raise typer.Exit(code=1)

    # 4. 계정 (네트워크)
    product = asyncio.run(_fetch_product())
    if product is None:
        problems.append("`GET /me` 실패 — 네트워크나 토큰을 확인하세요")
    elif product != "premium":
        problems.append(
            f"계정이 Premium 이 아닙니다 (product={product}).\n"
            "     신규 Client ID 와 /me/player 신뢰성이 Premium 을 전제합니다."
        )
    else:
        console.print("[green]OK[/green]  계정        premium")

    if problems:
        _report(problems)
        raise typer.Exit(code=1)
    console.print(
        "\n[green]준비 완료[/green] — `zsh scripts/launchd-install.sh` 로 상시 수집을 시작하세요."
    )


async def _fetch_product() -> str | None:
    from ..collect.runner import build_collector
    from ..collect.snapshots import snapshot_me

    collector = build_collector()
    try:
        me = await snapshot_me(collector.conn, collector.client)
        return None if me is None else str(me.get("product") or "unknown")
    except Exception:
        return None
    finally:
        await collector.aclose()


@app.command("status")
def status() -> None:
    """수집 현황 요약 (네트워크를 쓰지 않는다)."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        row = conn.execute(SELECT_LAST_SAMPLE_AT).fetchone()
    finally:
        conn.close()

    if row is None or row[0] is None:
        console.print("[yellow]아직 수집된 샘플이 없습니다.[/yellow]")
        return
    last = int(row[0])
    age_min = (now_ms() - last) / 60_000
    style = "green" if age_min < 10 else "yellow" if age_min < 60 else "red"
    console.print(
        f"마지막 샘플: [{style}]{epoch_ms_to_kst(last):%Y-%m-%d %H:%M:%S} KST "
        f"({age_min:.0f}분 전)[/{style}]"
    )


def _report(problems: list[str]) -> None:
    console.print("\n[red]해결해야 할 것:[/red]")
    for problem in problems:
        console.print(f"  ✗ {problem}")


@app.command("report")
def report_cmd(
    days: int = typer.Option(1, help="집계 기간(일)"),
    notify: bool = typer.Option(False, "--notify", help="Telegram 으로도 보낸다"),
) -> None:
    """수집 건강 리포트. 48시간 소크 테스트의 판정 근거다."""
    from ..report.daily import build_daily_report

    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        report = build_daily_report(conn, window_days=days)
    finally:
        conn.close()

    console.print(f"[bold]최근 {days}일[/bold]")
    for line in report.lines():
        console.print(f"  {line}")

    for warning in report.warnings:
        console.print(f"\n[yellow]주의[/yellow] {warning}")

    if notify:
        body = "\n".join(report.lines())
        if report.warnings:
            body += "\n\n주의:\n" + "\n".join(f"- {w}" for w in report.warnings)
        build_notifier(settings).send(
            AlertKind.DAILY_REPORT,
            "수집 리포트" + ("" if report.healthy else " (주의)"),
            body,
        )
    if not report.healthy:
        raise typer.Exit(code=1)


@app.command("telegram-chat-id")
def telegram_chat_id() -> None:
    """봇에게 온 메시지에서 `chat_id` 를 찾아 출력한다.

    브라우저로 `api.telegram.org/bot<TOKEN>/getUpdates` 를 여는 대신 이걸
    쓴다 — 그 URL 을 주소창에 넣으면 **브라우저 기록에 봇 토큰이 남는다**.
    여기서는 토큰이 화면에도 로그에도 나오지 않는다.
    """
    import httpx

    from ..notify.telegram import TELEGRAM_API_BASE

    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        console.print(
            "[red]TELEGRAM_BOT_TOKEN 이 비어 있습니다.[/red]\n"
            "@BotFather 에서 받은 토큰을 `.env` 에 먼저 넣으세요."
        )
        raise typer.Exit(code=1)

    try:
        resp = httpx.get(f"{TELEGRAM_API_BASE}/bot{token}/getUpdates", timeout=10.0)
    except httpx.HTTPError as exc:
        # 예외 문자열에 토큰이 담긴 URL 이 들어가므로 타입만 보여 준다.
        console.print(f"[red]요청 실패[/red] ({type(exc).__name__}) — 네트워크를 확인하세요.")
        raise typer.Exit(code=1) from None

    if resp.status_code == 401:
        console.print("[red]토큰이 거부되었습니다.[/red] `.env` 의 값을 다시 확인하세요.")
        raise typer.Exit(code=1)
    if resp.status_code >= 400:
        console.print(f"[red]Telegram 오류[/red] HTTP {resp.status_code}")
        raise typer.Exit(code=1)

    chats: dict[str, str] = {}
    for update in resp.json().get("result") or []:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
            chats[str(chat["id"])] = str(label)

    if not chats:
        console.print(
            "[yellow]받은 메시지가 없습니다.[/yellow]\n"
            "Telegram 에서 만든 봇을 열고 아무 메시지나 하나 보낸 뒤 다시 실행하세요.\n"
            "(getUpdates 는 봇이 받은 메시지가 있어야 chat 을 알려 줍니다.)"
        )
        raise typer.Exit(code=1)

    console.print("찾은 chat:")
    for chat_id, label in chats.items():
        console.print(f"  [cyan]{chat_id}[/cyan]  {label}")
    console.print("\n이 값을 `.env` 의 TELEGRAM_CHAT_ID 에 넣고:")
    console.print("  [cyan]uv run spotify-taste ops test-alert[/cyan]")
