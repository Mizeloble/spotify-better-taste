"""무엇/왜
DB 운영 명령. 마이그레이션 적용, 스키마 검증, 적재 현황 확인.

설계 원칙
- `migrate` 는 멱등이다. launchd 래퍼가 매 실행마다 불러도 안전해야 한다.
- `verify` 는 실패 시 **비영(非零) 종료 코드**를 낸다. 셸에서 게이트로 쓰인다.
- 표시 시각만 KST 로 바꾼다. 저장은 언제나 UTC epoch ms.

어떻게 확인
    uv run spotify-taste db migrate
    uv run spotify-taste db stats
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from .._common import epoch_ms_to_kst, now_ms
from ..config import get_settings
from ..db.connection import connect, tx
from ..db.maintenance import backup, database_size, integrity_check, prune_expired_raw
from ..db.migrate import current_version, discover, migrate, schema_fingerprint
from ..db.queries import COUNT_TABLE_ROWS, SELECT_COLLECTION_HEALTH, SELECT_TABLE_NAMES
from ..notify import AlertKind, build_notifier

app = typer.Typer(help="DB 마이그레이션과 상태 확인", no_args_is_help=True)
console = Console()


@app.command("migrate")
def migrate_cmd() -> None:
    """미적용 마이그레이션을 적용한다 (멱등)."""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        before = current_version(conn)
        applied = migrate(conn)
        after = current_version(conn)
    finally:
        conn.close()

    if not applied:
        console.print(f"[green]최신 상태[/green] (user_version={after}, {settings.db_path})")
        return
    for m in applied:
        console.print(f"  적용: [cyan]{m.version:04d}_{m.name}[/cyan]")
    console.print(f"[green]완료[/green] user_version {before} → {after}")


@app.command("verify")
def verify_cmd() -> None:
    """스키마가 마이그레이션이 만든 상태와 일치하는지 확인한다.

    손편집이나 중단된 마이그레이션을 잡는다. 어긋난 채로 수집이 도는 것이
    가장 나쁜 상태이므로, 실패하면 종료 코드 1 로 셸을 멈춘다.
    """
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        version = current_version(conn)
        fingerprint = schema_fingerprint(conn)
        tables = {r["name"] for r in conn.execute(SELECT_TABLE_NAMES)}
    finally:
        conn.close()

    expected_version = max((m.version for m in discover()), default=0)
    console.print(f"user_version : {version} (기대 {expected_version})")
    console.print(f"스키마 지문   : {fingerprint[:16]}…")
    console.print(f"테이블 수     : {len(tables)}")

    if version != expected_version:
        console.print(
            f"[red]불일치[/red] — `spotify-taste db migrate` 를 실행하세요 "
            f"(현재 {version}, 기대 {expected_version})"
        )
        raise typer.Exit(code=1)
    console.print("[green]정상[/green]")


@app.command("stats")
def stats_cmd() -> None:
    """테이블별 행 수와 수집 건강 지표를 출력한다."""
    settings = get_settings()
    if not settings.db_path.exists():
        console.print(f"[red]DB 가 없습니다:[/red] {settings.db_path}")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path, readonly=True)
    try:
        names = [r["name"] for r in conn.execute(SELECT_TABLE_NAMES)]
        counts = {
            name: conn.execute(COUNT_TABLE_ROWS.format(table=name)).fetchone()["n"]
            for name in names
        }
        health = dict(conn.execute(SELECT_COLLECTION_HEALTH).fetchone())
    finally:
        conn.close()

    table = Table(title=str(settings.db_path))
    table.add_column("테이블")
    table.add_column("행", justify="right")
    for name, count in counts.items():
        # 비어 있는 테이블은 흐리게 — Phase 2~6 테이블이 대부분 여기 해당한다.
        style = "dim" if count == 0 else ""
        table.add_row(name, f"{count:,}", style=style)
    console.print(table)

    last = health.get("last_sample_at")
    if last is None:
        console.print("[yellow]아직 수집된 샘플이 없습니다.[/yellow]")
        return

    age_min = (now_ms() - int(last)) / 60_000
    freshness = "green" if age_min < 10 else "yellow" if age_min < 60 else "red"
    console.print(
        f"마지막 샘플: [{freshness}]{epoch_ms_to_kst(int(last)):%Y-%m-%d %H:%M:%S} KST"
        f" ({age_min:.0f}분 전)[/{freshness}]"
    )
    console.print(
        f"세션: 폴러 {health['poller_session_count']:,} / "
        f"RP {health['rp_session_count']:,} / "
        f"열림 {health['open_session_count']:,} · "
        f"대체 {health['superseded_count']:,} · 팬텀 {health['phantom_count']:,} · "
        f"미조정 공백 {health['open_gap_count']:,}"
    )


@app.command("backup")
def backup_cmd() -> None:
    """`VACUUM INTO` 로 온라인 백업을 뜬다 (파일 복사는 WAL 때문에 깨진다)."""
    settings = get_settings()
    if settings.backup_dir is None:
        console.print("[yellow]BACKUP_DIR 이 비어 있어 백업이 비활성입니다.[/yellow]")
        raise typer.Exit(code=1)

    conn = connect(settings.db_path)
    try:
        result = backup(
            conn,
            settings.backup_dir,
            keep_daily=settings.backup_keep_daily,
            keep_weekly=settings.backup_keep_weekly,
        )
    finally:
        conn.close()
    console.print(f"[green]백업 완료[/green] {result.path} ({result.bytes_written / 1e6:.1f} MB)")
    if result.removed:
        console.print(f"오래된 스냅샷 {len(result.removed)}개 정리")


@app.command("prune")
def prune_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="지우지 않고 개수만 센다"),
) -> None:
    """만료된 원본을 지운다. `keep_until IS NULL` 은 절대 건드리지 않는다."""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        if dry_run:
            count = prune_expired_raw(conn, dry_run=True)
            console.print(f"[yellow]dry-run[/yellow] — 만료 대상 {count:,}건")
        else:
            with tx(conn):
                count = prune_expired_raw(conn)
            console.print(f"[green]정리 완료[/green] {count:,}건")
        console.print(f"DB 사용량: {database_size(settings.db_path) / 1e6:.1f} MB")
    finally:
        conn.close()


@app.command("integrity-check")
def integrity_cmd() -> None:
    """`PRAGMA integrity_check`. 실패하면 알림을 보내고 비영으로 종료한다."""
    settings = get_settings()
    conn = connect(settings.db_path, readonly=True)
    try:
        ok, detail = integrity_check(conn)
    finally:
        conn.close()

    if ok:
        console.print("[green]정상[/green]")
        return
    console.print(f"[red]손상 감지[/red]\n{detail}")
    build_notifier(settings).send(
        AlertKind.DB_INTEGRITY,
        "DB 무결성 점검 실패",
        f"{settings.db_path}\n\n{detail[:1000]}\n\n최근 백업으로 복구하세요: data/backups/",
    )
    raise typer.Exit(code=1)
