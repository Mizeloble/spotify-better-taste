"""무엇/왜
야간 유지보수. 백업, 만료된 원본 정리, 무결성 점검.

설계 원칙
- **백업은 `VACUUM INTO`.** SQLite 의 올바른 온라인 백업 방법이고 한 문장이다.
  파일을 `cp` 하면 WAL 이 진행 중일 때 깨진 스냅샷을 얻는다.
- **`keep_until IS NULL` 은 절대 지우지 않는다.** 저volume 엔드포인트 원문과
  '처음 보는 응답 모양'의 증거가 거기 있다. 다음 필드 삭제가 왔을 때
  재구성할 유일한 근거다.
- 무결성 점검 실패는 조용히 넘어가면 안 된다 — 알림으로 올린다.

어떻게 확인
    uv run spotify-taste db backup
    uv run pytest tests/test_maintenance.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import structlog

from .._common import epoch_ms_to_kst, now_ms
from .queries import DELETE_EXPIRED_RAW

__all__ = ["BackupResult", "backup", "integrity_check", "prune_expired_raw"]

log = structlog.get_logger(__name__)


@dataclass
class BackupResult:
    path: Path
    bytes_written: int
    removed: list[Path]


def backup(
    conn: sqlite3.Connection,
    backup_dir: Path,
    *,
    keep_daily: int = 7,
    keep_weekly: int = 8,
) -> BackupResult:
    """`VACUUM INTO` 로 스냅샷을 뜨고 오래된 것을 정리한다."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    today = epoch_ms_to_kst(now_ms())
    target = backup_dir / f"spotify-{today:%Y-%m-%d}.db"
    target.unlink(missing_ok=True)

    # VACUUM 은 트랜잭션 안에서 실행할 수 없다. 커넥션이 autocommit 이고
    # `tx()` 밖이므로 열린 트랜잭션이 없다 — 그대로 실행하면 된다.
    # 파라미터 바인딩이 안 되는 문장이다. 경로는 우리가 만든 것이라 안전.
    conn.execute(f"VACUUM INTO '{target}'")

    size = target.stat().st_size
    removed = _prune_backups(backup_dir, keep_daily=keep_daily, keep_weekly=keep_weekly)
    log.info("db.backup", path=str(target), bytes=size, removed=len(removed))
    return BackupResult(path=target, bytes_written=size, removed=removed)


def _prune_backups(backup_dir: Path, *, keep_daily: int, keep_weekly: int) -> list[Path]:
    """최근 N일치는 전부, 그 이전은 주 1개(월요일)만 남긴다.

    이름이 규칙에 맞지 않는 파일은 건드리지 않는다 — 사람이 손으로 떠 둔
    스냅샷을 정리 작업이 지우면 안 된다.
    """
    snapshots = sorted(backup_dir.glob("spotify-*.db"), reverse=True)
    keep: set[Path] = set(snapshots[:keep_daily])

    weekly_kept = 0
    for path in snapshots[keep_daily:]:
        parsed = _date_from_name(path)
        if parsed is None:
            keep.add(path)
            continue
        if parsed.weekday() == 0 and weekly_kept < keep_weekly:
            keep.add(path)
            weekly_kept += 1

    removed: list[Path] = []
    for path in snapshots:
        if path not in keep:
            path.unlink(missing_ok=True)
            removed.append(path)
    return removed


def _date_from_name(path: Path) -> date | None:
    """`spotify-2026-08-09.db` → date(2026, 8, 9). 규칙 밖이면 None."""
    stamp = path.stem.removeprefix("spotify-")
    try:
        return datetime.strptime(stamp, "%Y-%m-%d").date()
    except ValueError:
        return None


def prune_expired_raw(conn: sqlite3.Connection, *, dry_run: bool = False) -> int:
    """만료된 원본을 지운다. `keep_until IS NULL` 은 손대지 않는다."""
    now = now_ms()
    if dry_run:
        row = conn.execute(
            "SELECT count(*) FROM raw_api_response WHERE keep_until IS NOT NULL AND keep_until < ?",
            (now,),
        ).fetchone()
        return int(row[0])
    cur = conn.execute(DELETE_EXPIRED_RAW, (now,))
    deleted = cur.rowcount
    log.info("db.raw_pruned", deleted=deleted)
    return deleted


def integrity_check(conn: sqlite3.Connection) -> tuple[bool, str]:
    """`PRAGMA integrity_check`. (정상인가, 상세) 를 돌려준다."""
    rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check")]
    detail = "\n".join(rows)
    return (rows == ["ok"], detail)


def database_size(path: Path) -> int:
    """WAL 을 포함한 실제 사용량."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            total += candidate.stat().st_size
    return total
