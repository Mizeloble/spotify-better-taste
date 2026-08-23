"""무엇/왜
취향 특성 계산 명령. 파라미터 버전을 만들고, 0·1층을 적재하고, 결과를 본다.

어떻게 확인
    uv run spotify-taste features params --new
    uv run spotify-taste features build --write
    uv run spotify-taste features top artist --horizon core
    uv run spotify-taste features behavior --write
"""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from ..config import get_settings
from ..db.connection import connect
from ..db.queries import SELECT_LATEST_FEATURE_PARAMS, SELECT_TOP_AFFINITY
from ..features import (
    build_behavior_profile,
    build_features,
    create_params_version,
    load_params,
)

app = typer.Typer(help="취향 특성 (0층 가중치 · 1층 친화도 · 3층 행동)", no_args_is_help=True)
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


#: 이보다 표본이 적은 기간은 눈에 띄게 표시한다. 숨기지는 않는다 —
#: 03시에 4건뿐이라는 사실도 그 자체로 프로파일이다.
LOW_SAMPLE = 30

#: 요일 이름. `dow:0` 이 일요일 (sqlite `strftime('%w')` 관례).
DOW_NAMES = ("일", "월", "화", "수", "목", "금", "토")


@app.command("behavior")
def behavior(
    write: bool = typer.Option(False, "--write", help="실제로 적재한다"),
    params_version: int = typer.Option(None, "--params-version", help="기본값은 최신"),
    source: str = typer.Option("all", "--source", help="all | measured"),
    by: str = typer.Option("", "--by", help="hour | dow — 비우면 전 구간 요약"),
) -> None:
    """3층 행동 프로파일을 계산한다."""
    if by not in ("", "hour", "dow"):
        console.print("[red]--by 는 hour 또는 dow 입니다.[/red]")
        raise typer.Exit(code=1)

    conn = _open(readonly=not write)
    try:
        result = build_behavior_profile(conn, params_version=params_version, write=write)
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    console.print(f"파라미터   : 버전 {result.params_version}")
    console.print(f"재생       : {result.sessions:,}  (실측 {result.measured:,})")
    console.print(f"지표       : {len(result.metrics):,}행 · 기간 {result.periods}개")

    # 저장된 행이 아니라 방금 계산한 것을 보여 준다.
    rows = [m for m in result.metrics if m.source_filter == source]
    if not rows:
        console.print(f"[yellow]{source} 벌에 지표가 없습니다.[/yellow]")
    elif by:
        _print_periods([m for m in rows if m.period.startswith(f"{by}:")], by=by, source=source)
    else:
        _print_summary([m for m in rows if m.period == "all"], source=source)

    if not write:
        console.print("[yellow]dry-run[/yellow] — 저장하려면 `--write` 를 붙이세요.")


def _print_summary(metrics, *, source: str) -> None:
    table = Table(title=f"행동 프로파일 · 전 구간 · {source}")
    table.add_column("지표")
    table.add_column("값", justify="right")
    table.add_column("표본", justify="right")
    for m in sorted(metrics, key=lambda m: m.metric):
        table.add_row(m.metric, _fmt(m.metric, m.value), f"{m.sample_size:,}")
    console.print(table)
    if source == "measured":
        # 실측만 남기면 백필된 트랙만 남는데, 백필은 재생 횟수 순이다.
        console.print(
            "[yellow]주의[/yellow] measured 벌의 exploration_rate 는 믿지 말 것 — "
            "롱테일이 통째로 빠져 있습니다."
        )


def _print_periods(metrics, *, by: str, source: str) -> None:
    """기간별로는 재생 몫과 완주율을 나란히 본다 — 언제 많이 듣나와 그때
    어떻게 듣나는 다른 질문이고, 둘이 어긋나는 지점이 재미있는 자리다."""
    label = "시간대" if by == "hour" else "요일"
    share = {m.period: m for m in metrics if m.metric == "play_share"}
    completion = {m.period: m.value for m in metrics if m.metric == "completion_mean"}
    explore = {m.period: m.value for m in metrics if m.metric == "exploration_rate"}

    table = Table(title=f"행동 프로파일 · {label}별 · {source}")
    table.add_column(label)
    table.add_column("재생", justify="right")
    table.add_column("몫", justify="right")
    table.add_column("완주", justify="right")
    table.add_column("탐색", justify="right")
    table.add_column("", justify="left")
    for period in sorted(share):
        m = share[period]
        key = period.split(":", 1)[1]
        name = f"{DOW_NAMES[int(key)]}요일" if by == "dow" else f"{key}시"
        low = m.sample_size < LOW_SAMPLE
        table.add_row(
            name,
            f"{m.sample_size:,}" + ("*" if low else ""),
            f"{m.value * 100:.1f}%",
            _pct(completion.get(period)),
            _pct(explore.get(period)),
            "█" * round(m.value * 50),
        )
    console.print(table)
    console.print(f"[dim]* 표본 {LOW_SAMPLE}건 미만 — 값을 믿지 말 것[/dim]")


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _fmt(metric: str, value: float) -> str:
    if metric == "play_count":
        return f"{int(value):,}"
    if metric.endswith(("_rate", "_share")):
        return f"{value * 100:.1f}%"
    return f"{value:.3f}"
