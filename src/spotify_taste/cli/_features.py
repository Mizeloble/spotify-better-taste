"""무엇/왜
취향 특성 계산 명령. 파라미터 버전을 만들고, 0·1층을 적재하고, 결과를 본다.

어떻게 확인
    uv run spotify-taste features params --new
    uv run spotify-taste features build --write
    uv run spotify-taste features top artist --horizon core
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from ..config import get_settings
from ..db.connection import connect
from ..db.queries import SELECT_LATEST_FEATURE_PARAMS, SELECT_TOP_AFFINITY
from ..features import build_features, create_params_version, load_params

app = typer.Typer(help="취향 특성 (0층 가중치 · 1층 친화도)", no_args_is_help=True)
console = Console()


def _open(readonly: bool = False):
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)
    return connect(settings.db_path, readonly=readonly)


@app.command("params")
def params(
    new: bool = typer.Option(False, "--new", help="현재 데이터에서 기저율을 유도해 새 버전 생성"),
    note: str = typer.Option("", help="이 버전을 왜 만들었는지"),
) -> None:
    """파라미터 버전을 보거나 만든다."""
    conn = _open(readonly=not new)
    try:
        if new:
            version = create_params_version(conn, note=note or None)
            console.print(f"[green]버전 {version} 생성[/green]")
        row = conn.execute(SELECT_LATEST_FEATURE_PARAMS).fetchone()
        if row is None:
            console.print("파라미터 버전이 없습니다 — `--new` 로 만드세요.")
            raise typer.Exit(code=1)
        data = json.loads(row["params_json"])
        console.print(f"[bold]버전 {row['version']}[/bold] {row['note'] or ''}")
        console.print(
            f"  반감기      : current {data['half_life_days_current']:.0f}일 / "
            f"core {data['half_life_days_core']:.0f}일"
        )
        console.print(f"  전체 기저율 : {data['global_base_rate']:.3f}")
        rates = data.get("mode_base_rates") or {}
        for mode, rate in sorted(rates.items(), key=lambda kv: -kv[1]):
            console.print(f"    {mode:<12} {rate:.3f}")
    finally:
        conn.close()


@app.command("build")
def build(
    write: bool = typer.Option(False, "--write", help="실제로 적재한다"),
    params_version: int = typer.Option(None, "--params-version", help="기본값은 최신"),
) -> None:
    """0층 가중치와 1층 친화도를 계산한다."""
    conn = _open()
    try:
        result = build_features(conn, params_version=params_version, write=write)
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print(f"파라미터   : 버전 {result.params_version}")
    console.print(f"세션       : {result.sessions:,}")
    console.print(f"  가중됨   : {result.weighted:,}")
    # 완주도를 모르는 재생은 0 이 아니라 '모름'이다. 그 규모를 숨기지 않는다.
    console.print(f"  완주도 모름: {result.unknown_completion:,} (백필 대기)")
    if write:
        console.print(
            f"친화도     : 아티스트 {result.artists:,} · 트랙 {result.tracks:,} · "
            f"앨범 {result.albums:,}"
        )
    else:
        console.print("[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")


@app.command("top")
def top(
    entity_type: str = typer.Argument("artist", help="artist | track | album"),
    horizon: str = typer.Option("core", help="core(24개월) | current(6개월)"),
    limit: int = typer.Option(20),
    params_version: int = typer.Option(None, "--params-version"),
) -> None:
    """친화도 상위."""
    conn = _open(readonly=True)
    try:
        version = params_version or load_params(conn)[0]
        rows = conn.execute(SELECT_TOP_AFFINITY, (entity_type, horizon, version, limit)).fetchall()
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    if not rows:
        console.print("결과가 없습니다 — `features build --write` 를 먼저 실행하세요.")
        raise typer.Exit(code=1)

    table = Table(title=f"{entity_type} 친화도 · {horizon} · 파라미터 v{version}")
    table.add_column("이름")
    table.add_column("점수", justify="right")
    table.add_column("재생", justify="right")
    # 실측 비중을 함께 보여 준다 — 추정에 기댄 점수를 같은 눈으로 보면 안 된다.
    table.add_column("실측", justify="right")
    for row in rows:
        table.add_row(
            row["name"] or row["entity_key"],
            f"{row['score']:.1f}",
            f"{row['plays']:,}",
            f"{row['measured_plays']:,}",
        )
    console.print(table)
