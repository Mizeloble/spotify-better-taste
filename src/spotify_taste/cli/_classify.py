"""무엇/왜
분류 명령. 규칙을 바꾼 뒤 과거를 다시 계산하고, 그 결과가 건강한지 본다.

설계 원칙
- **`--dry-run` 이 기본값이다.** 전이 행렬을 먼저 보고 쓴다. 규칙 하나를
  고치는 것이 수천 행의 의미를 바꾸는 일이라 그렇다.
- `report` 는 null-context 비율을 보여 준다. 80% 를 넘으면 사용자 행동이
  아니라 계정 증상이고, 성능저하 모드를 검토해야 한다.

어떻게 확인
    uv run spotify-taste classify run --dry-run
    uv run spotify-taste classify report
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from .._common import iso_to_epoch_ms
from ..classify.patterns import RULES_VERSION
from ..classify.playlist_cache import refresh_playlist_cache
from ..classify.recompute import recompute
from ..config import get_settings
from ..db.connection import connect, tx
from ..db.queries import (
    SELECT_NULL_CONTEXT_RATIO,
    SELECT_ORIGIN_DISTRIBUTION,
    SELECT_TOP_UNRESOLVED_PLAYLISTS,
)

app = typer.Typer(help="재생 출처 분류", no_args_is_help=True)
console = Console()

#: 이 비율을 넘으면 계정 증상으로 본다 (사용자 행동이 아니라).
NULL_CONTEXT_ALARM = 0.80


@app.command("run")
def run(
    stale: bool = typer.Option(True, "--stale/--all", help="바뀐 규칙 대상만/전부"),
    since: str = typer.Option("", help="이 날짜 이후만 (YYYY-MM-DD, KST)"),
    # dry-run 이 기본값이다. `--dry-run` 을 명시적으로 받아 두는 이유는
    # 문서와 습관이 그 형태이기 때문 — 없으면 사용법 오류로 떨어진다.
    dry_run: bool = typer.Option(True, "--dry-run/--write", help="저장하지 않고 전이만 본다"),
    resolve: bool = typer.Option(
        True, "--resolve/--no-resolve", help="모르는 플레이리스트 소유자를 조회한다"
    ),
) -> None:
    """세션의 출처를 다시 판정한다. 기본은 dry-run."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path)
    try:
        if resolve:
            _resolve_playlists(conn)

        if dry_run:
            result = recompute(
                conn,
                stale_only=stale,
                since=_day_to_ms(since) if since else None,
                write=False,
                degraded_mode=settings.context_null_account,
            )
        else:
            with tx(conn):
                result = recompute(
                    conn,
                    stale_only=stale,
                    since=_day_to_ms(since) if since else None,
                    write=True,
                    degraded_mode=settings.context_null_account,
                )
    finally:
        conn.close()

    console.print(f"규칙 버전 : {RULES_VERSION}")
    console.print(f"대상      : {result.considered:,}")
    console.print(f"변경      : {result.changed:,}")

    if result.transitions:
        table = Table(title="전이 행렬")
        table.add_column("이전")
        table.add_column("이후")
        table.add_column("행", justify="right")
        for before, after, count in result.transition_rows():
            table.add_row(before, after, f"{count:,}")
        console.print(table)

    if dry_run:
        console.print("[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")
    else:
        console.print(f"[green]저장됨[/green] : {result.written:,}")


def _resolve_playlists(conn) -> None:
    """모르는 플레이리스트 소유자를 채운다 (네트워크를 쓰는 유일한 단계)."""
    from ..collect.runner import build_collector

    collector = build_collector()
    try:
        resolved = asyncio.run(refresh_playlist_cache(conn, collector.client))
        if resolved:
            console.print(f"플레이리스트 소유자 {resolved}건 확인")
    except Exception as exc:  # 분류 자체는 네트워크 없이도 되어야 한다
        console.print(f"[yellow]플레이리스트 조회 생략[/yellow] ({type(exc).__name__})")
    finally:
        asyncio.run(collector.aclose())


@app.command("report")
def report(top: int = typer.Option(10, help="미해결 플레이리스트 상위 N개")) -> None:
    """분류 결과의 건강 상태."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        distribution = conn.execute(SELECT_ORIGIN_DISTRIBUTION).fetchall()
        ratio_row = conn.execute(SELECT_NULL_CONTEXT_RATIO).fetchone()
        unresolved = conn.execute(SELECT_TOP_UNRESOLVED_PLAYLISTS, (top,)).fetchall()
    finally:
        conn.close()

    total = int(ratio_row["total"] or 0)
    if total == 0:
        console.print("[yellow]아직 분류할 세션이 없습니다.[/yellow]")
        return

    table = Table(title="출처 분포")
    table.add_column("origin")
    table.add_column("세션", justify="right")
    table.add_column("비율", justify="right")
    for row in distribution:
        table.add_row(row["origin"], f"{row['n']:,}", f"{row['n'] / total:.1%}")
    console.print(table)

    null_ratio = int(ratio_row["null_context"] or 0) / total
    style = "red" if null_ratio > NULL_CONTEXT_ALARM else "green"
    console.print(f"null context : [{style}]{null_ratio:.1%}[/{style}]")
    if null_ratio > NULL_CONTEXT_ALARM:
        console.print(
            "[red]경고[/red] — 이 비율은 사용자 행동이 아니라 계정 증상입니다.\n"
            "  `.env` 에 CONTEXT_NULL_ACCOUNT=true 를 검토하세요. 이 계정에서는\n"
            "  algorithmic 재현율이 0 에 가깝고, Discover Weekly 와의 비교가\n"
            "  성립하지 않을 수 있습니다."
        )

    if unresolved:
        console.print("\n미해결 플레이리스트 (소유자 확인 필요):")
        for row in unresolved:
            console.print(f"  {row['n']:>5,}  {row['context_uri']}")


def _day_to_ms(day: str) -> int:
    return iso_to_epoch_ms(f"{day}T00:00:00+09:00")
