"""무엇/왜
백필과 스냅샷 명령.

어떻게 확인
    uv run spotify-taste backfill run --max-calls 5
    uv run spotify-taste backfill status
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from ..collect.runner import build_collector
from ..collect.snapshots import (
    snapshot_library,
    snapshot_me,
    snapshot_playlists,
    snapshot_top,
)
from ..config import get_settings
from ..db.connection import connect
from ..db.queries import SELECT_BACKFILL_STATUS
from ..metadata.backfill import run_backfill

backfill_app = typer.Typer(help="카탈로그 메타데이터 백필", no_args_is_help=True)
snapshot_app = typer.Typer(help="라이브러리·플레이리스트·top 스냅샷", no_args_is_help=True)
console = Console()

#: 야간 예산제 실행에서 하루에 얼마나 흘릴지의 기본값.
DEFAULT_MAX_CALLS = 3000


@backfill_app.command("run")
def run(
    max_calls: int = typer.Option(DEFAULT_MAX_CALLS, help="이번 실행의 호출 예산"),
) -> None:
    """카탈로그를 예산 안에서 채운다 (재개 가능)."""
    collector = build_collector()
    try:
        result = asyncio.run(run_backfill(collector.conn, collector.client, max_calls=max_calls))
    finally:
        asyncio.run(collector.aclose())

    console.print(f"호출      : {result.calls:,} / {max_calls:,}")
    console.print(
        f"해결      : 트랙 {result.tracks:,} · 아티스트 {result.artists:,} · 앨범 {result.albums:,}"
    )
    if result.not_found:
        console.print(f"없는 항목 : {result.not_found:,} (다시 시도하지 않음)")
    if result.errors:
        console.print(f"[yellow]오류[/yellow]      : {result.errors:,} (다음 실행에 재시도)")
    if result.stopped_reason != "budget":
        console.print(f"[yellow]중단 사유[/yellow] : {result.stopped_reason}")


@backfill_app.command("status")
def status() -> None:
    """큐 깊이와 예상 완료일."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        row = dict(conn.execute(SELECT_BACKFILL_STATUS).fetchone())
    finally:
        conn.close()

    table = Table(title="백필 큐")
    table.add_column("종류")
    table.add_column("완료", justify="right")
    table.add_column("대기", justify="right")
    for label, ok_key, pending_key in (
        ("트랙", "tracks_ok", "tracks_pending"),
        ("아티스트", "artists_ok", "artists_pending"),
        ("앨범", "albums_ok", "albums_pending"),
    ):
        table.add_row(label, f"{row[ok_key]:,}", f"{row[pending_key]:,}")
    console.print(table)

    pending = row["tracks_pending"] + row["artists_pending"] + row["albums_pending"]
    if pending == 0:
        console.print("[green]큐가 비었습니다.[/green]")
    else:
        nights = -(-pending // DEFAULT_MAX_CALLS)  # 올림
        console.print(f"남은 항목 : {pending:,}  → 야간 실행 기준 약 {nights}일")
    if row["tracks_missing"]:
        console.print(f"없는 트랙 : {row['tracks_missing']:,} (영구 표시)")


@snapshot_app.command("all")
def snapshot_all() -> None:
    """`me` → 라이브러리 → 플레이리스트 → top 을 순서대로."""
    collector = build_collector()

    async def _run() -> tuple[int, int, int]:
        await snapshot_me(collector.conn, collector.client)
        library = await snapshot_library(collector.conn, collector.client)
        playlists = await snapshot_playlists(collector.conn, collector.client)
        # /me/top 은 현재 상태만 준다 — 찍지 않은 주는 영구 소실이다.
        top = await snapshot_top(collector.conn, collector.client)
        return library, playlists, top

    try:
        library, playlists, top = asyncio.run(_run())
    finally:
        asyncio.run(collector.aclose())

    console.print(f"라이브러리   : {library:,}곡")
    console.print(f"플레이리스트 : {playlists:,}개")
    console.print(f"top 항목     : {top:,}행")


@snapshot_app.command("library")
def snapshot_library_cmd() -> None:
    """`GET /me/tracks` 만."""
    collector = build_collector()
    try:
        count = asyncio.run(snapshot_library(collector.conn, collector.client))
    finally:
        asyncio.run(collector.aclose())
    console.print(f"라이브러리 {count:,}곡")


@snapshot_app.command("top")
def snapshot_top_cmd() -> None:
    """`GET /me/top/*` 만. 찍지 않은 주는 영구 소실이다."""
    collector = build_collector()
    try:
        count = asyncio.run(snapshot_top(collector.conn, collector.client))
    finally:
        asyncio.run(collector.aclose())
    console.print(f"top {count:,}행")


@snapshot_app.command("me")
def snapshot_me_cmd() -> None:
    """`GET /me`. 분류기가 `owner.id == me` 를 판정하려면 필요하다."""
    collector = build_collector()
    try:
        me = asyncio.run(snapshot_me(collector.conn, collector.client))
    finally:
        asyncio.run(collector.aclose())
    if me is None:
        console.print("[red]조회 실패[/red]")
        raise typer.Exit(code=1)
    console.print(f"{me.get('display_name')} ({me.get('id')}) · product={me.get('product')}")
