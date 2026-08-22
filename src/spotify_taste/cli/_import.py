"""무엇/왜
일회성 대량 임포트 명령. Extended Streaming History 와 기술 로그.

어떻게 확인
    uv run spotify-taste import history              # dry-run
    uv run spotify-taste import history --write
    uv run spotify-taste import techlog --dir ~/Downloads/…  # dry-run
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..config import get_settings
from ..db.connection import connect, tx
from ..importer.extended_history import find_audio_files, import_extended_history
from ..importer.techlog import discover_files, import_techlog

app = typer.Typer(help="일회성 대량 임포트", no_args_is_help=True)
console = Console()

DEFAULT_DIR = Path("data/import/extended-history")
TECHLOG_DIR = Path("data/import/technical-log")


@app.command("history")
def history(
    directory: Path = typer.Option(
        DEFAULT_DIR, "--dir", help="export ZIP 을 푼 디렉터리 (하위까지 검색)"
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--write", help="저장하지 않고 통계만 본다"),
) -> None:
    """Extended Streaming History 를 play_session 으로 옮긴다 (멱등)."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print("[red]DB 가 없습니다:[/red] `db migrate` 를 먼저 실행하세요.")
        raise typer.Exit(code=1)

    if not find_audio_files(directory):
        console.print(
            f"[red]Streaming_History_Audio_*.json 을 찾지 못했습니다:[/red] {directory}\n"
            "export ZIP 을 이 디렉터리에 풀었는지 확인하세요."
        )
        raise typer.Exit(code=1)

    conn = connect(settings.db_path)
    try:
        if dry_run:
            stats = import_extended_history(conn, directory, write=False)
        else:
            with tx(conn):
                stats = import_extended_history(conn, directory, write=True)
    finally:
        conn.close()

    for line in stats.lines():
        console.print(f"  {line}")

    if dry_run:
        console.print("\n[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")
        return

    console.print("\n[green]임포트 완료[/green]")
    console.print(
        f"고유 트랙 {len(stats.distinct_tracks):,}개가 백필 큐에 들어갑니다.\n"
        "야간 잡이 하루 3,000콜씩 채우지만, 지금 확인하려면:\n"
        "  [cyan]uv run spotify-taste backfill status[/cyan]\n"
        "분류는 오늘 밤 04:40 에 자동으로 돌거나, 직접:\n"
        "  [cyan]uv run spotify-taste classify run --dry-run[/cyan]"
    )


@app.command("techlog")
def techlog(
    directory: Path = typer.Option(
        TECHLOG_DIR, "--dir", help="Technical Log Information 을 푼 디렉터리"
    ),
    dry_run: bool = typer.Option(True, "--dry-run/--write", help="저장하지 않고 통계만 본다"),
) -> None:
    """기술 로그에서 컨텍스트·곡 길이·알고리즘 노출을 가져온다 (멱등).

    재생 사실은 거의 늘지 않는다 — 대부분 이미 history_export 로 들어와 있고,
    이 임포트는 거기에 컨텍스트와 곡 길이를 **붙인다**.
    """
    settings = get_settings()
    if not settings.db_path.exists():
        console.print("[red]DB 가 없습니다:[/red] `db migrate` 를 먼저 실행하세요.")
        raise typer.Exit(code=1)

    directory = directory.expanduser()
    if "stream" not in discover_files(directory):
        console.print(
            f"[red]RawCoreStream.json 을 찾지 못했습니다:[/red] {directory}\n"
            "기술 로그 ZIP 을 이 디렉터리에 풀었는지 확인하세요."
        )
        raise typer.Exit(code=1)

    conn = connect(settings.db_path)
    try:
        if dry_run:
            stats = import_techlog(conn, directory, write=False)
        else:
            with tx(conn):
                stats = import_techlog(conn, directory, write=True)
    finally:
        conn.close()

    for line in stats.lines():
        console.print(f"  {line}")

    if dry_run:
        console.print("\n[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")
        return

    console.print("\n[green]임포트 완료[/green]")
    console.print(
        "귀속을 판정에 반영하려면 분류를 다시 돌려야 합니다:\n"
        "  [cyan]uv run spotify-taste classify run --dry-run[/cyan]\n"
        "RULES_VERSION 이 올라갔으므로 전 행이 대상입니다."
    )
