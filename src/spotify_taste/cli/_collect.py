"""무엇/왜
수집 명령. 상시 수집과 진단용 1틱 실행, 그리고 세션 재계산.

어떻게 확인
    uv run spotify-taste collect --once
    uv run spotify-taste sessions rebuild --dry-run
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console

from .._common import epoch_ms_to_kst, iso_to_epoch_ms
from ..collect.runner import build_collector, run_forever, run_once
from ..config import get_settings
from ..db.connection import connect, tx
from ..sessions.rebuild import rebuild_sessions
from ..spotify.auth import SpotifyAuthRevoked

console = Console()

collect_app = typer.Typer(help="청취 로그 수집")
sessions_app = typer.Typer(help="세션 재계산", no_args_is_help=True)


@collect_app.callback(invoke_without_command=True)
def collect(
    ctx: typer.Context,
    once: bool = typer.Option(False, "--once", help="한 틱만 수집하고 종료한다"),
) -> None:
    """`/me/player` 와 recently-played 를 한 프로세스에서 수집한다."""
    if ctx.invoked_subcommand is not None:
        return

    collector = build_collector()
    try:
        if once:
            asyncio.run(run_once(collector))
            console.print("[green]1틱 수집 완료[/green] — `db stats` 로 확인하세요.")
            return
        code = asyncio.run(run_forever(collector))
        if code:
            raise typer.Exit(code=code)
    except SpotifyAuthRevoked as exc:
        console.print(
            f"[red]재인증이 필요합니다.[/red] {exc}\n  [cyan]uv run spotify-taste auth login[/cyan]"
        )
        raise typer.Exit(code=3) from exc
    finally:
        asyncio.run(collector.aclose())


@sessions_app.command("rebuild")
def rebuild(
    since: str = typer.Option("", help="시작일 (YYYY-MM-DD, KST). 비우면 전체"),
    until: str = typer.Option("", help="종료일 (YYYY-MM-DD, KST)"),
    dry_run: bool = typer.Option(True, "--dry-run/--write", help="저장하지 않고 결과만 본다"),
) -> None:
    """저장된 샘플에서 세션을 다시 계산한다.

    기본은 dry-run 이다 — 판정 규칙을 바꿨을 때 무엇이 달라지는지 먼저 본다.
    """
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    start = _day_to_ms(since) if since else 0
    end = _day_to_ms(until, end_of_day=True) if until else None

    conn = connect(settings.db_path)
    try:
        if dry_run:
            result = rebuild_sessions(conn, since=start, until=end, write=False)
        else:
            with tx(conn):
                result = rebuild_sessions(conn, since=start, until=end, write=True)
    finally:
        conn.close()

    console.print(f"샘플     : {result.sample_count:,}")
    console.print(f"세션     : {len(result.sessions):,}")
    if dry_run:
        console.print("[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")
    else:
        console.print(f"[green]저장됨[/green] : {result.written:,}")

    if result.sessions:
        first, last = result.sessions[0], result.sessions[-1]
        console.print(
            f"구간     : {epoch_ms_to_kst(first.started_at):%Y-%m-%d %H:%M} "
            f"~ {epoch_ms_to_kst(last.ended_at):%Y-%m-%d %H:%M} KST"
        )


def _day_to_ms(day: str, *, end_of_day: bool = False) -> int:
    """`YYYY-MM-DD` (KST) → UTC epoch ms."""
    suffix = "T23:59:59+09:00" if end_of_day else "T00:00:00+09:00"
    return iso_to_epoch_ms(f"{day}{suffix}")
