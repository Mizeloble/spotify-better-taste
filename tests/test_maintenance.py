"""백업·정리·리포트.

지키는 불변식: **keep_until IS NULL 은 절대 지우지 않는다**(필드 삭제가
왔을 때 재구성할 유일한 근거이기 때문).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from spotify_taste._common import now_ms
from spotify_taste.cli import app
from spotify_taste.db.connection import tx
from spotify_taste.db.maintenance import (
    backup,
    database_size,
    integrity_check,
    prune_expired_raw,
)
from spotify_taste.report.daily import build_daily_report

runner = CliRunner()
T0 = 1_800_000_000_000


def _raw(db: sqlite3.Connection, *, keep_until: int | None) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO raw_api_response (fetched_at, endpoint, http_status, body, "
            "source_job, keep_until) VALUES (?, '/me/player', 200, '{}', 'poller', ?)",
            (now_ms(), keep_until),
        )


def _session(db: sqlite3.Connection, **over) -> None:
    row: dict = {
        "track_uri": "spotify:track:A",
        "started_at": now_ms() - 3_600_000,
        "ended_at": now_ms() - 3_400_000,
        "source": "poller",
        "timing_confidence": "high",
        "created_at": T0,
        "updated_at": T0,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(["?"] * len(row))
    with tx(db):
        db.execute(f"INSERT INTO play_session ({cols}) VALUES ({marks})", tuple(row.values()))


# --- 백업 -----------------------------------------------------------------


def test_backup_creates_a_readable_snapshot(db: sqlite3.Connection, tmp_path: Path) -> None:
    _session(db)
    result = backup(db, tmp_path / "backups")
    assert result.path.exists() and result.bytes_written > 0

    restored = sqlite3.connect(result.path)
    try:
        assert restored.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1
    finally:
        restored.close()


def test_backup_prunes_old_daily_snapshots(db: sqlite3.Connection, tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    # 화요일들 — 주간 보관 대상이 아니다.
    for day in range(10, 25):
        (backup_dir / f"spotify-2020-03-{day:02d}.db").write_bytes(b"x")
    result = backup(db, backup_dir, keep_daily=3, keep_weekly=2)
    assert result.removed


def test_backup_never_touches_foreign_files(db: sqlite3.Connection, tmp_path: Path) -> None:
    """사람이 손으로 떠 둔 스냅샷을 정리 작업이 지우면 안 된다."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    manual = backup_dir / "spotify-before-migration.db"
    manual.write_bytes(b"precious")
    for day in range(1, 20):
        (backup_dir / f"spotify-2020-04-{day:02d}.db").write_bytes(b"x")
    backup(db, backup_dir, keep_daily=2, keep_weekly=1)
    assert manual.exists()


# --- 원본 정리 ------------------------------------------------------------


def test_expired_raw_is_deleted(db: sqlite3.Connection) -> None:
    _raw(db, keep_until=now_ms() - 1_000)
    with tx(db):
        assert prune_expired_raw(db) == 1
    assert db.execute("SELECT count(*) FROM raw_api_response").fetchone()[0] == 0


def test_permanent_raw_is_never_deleted(db: sqlite3.Connection) -> None:
    """저volume 원문과 '처음 보는 모양'의 증거가 여기 있다."""
    _raw(db, keep_until=None)
    with tx(db):
        prune_expired_raw(db)
    assert db.execute("SELECT count(*) FROM raw_api_response").fetchone()[0] == 1


def test_unexpired_raw_is_kept(db: sqlite3.Connection) -> None:
    _raw(db, keep_until=now_ms() + 86_400_000)
    with tx(db):
        prune_expired_raw(db)
    assert db.execute("SELECT count(*) FROM raw_api_response").fetchone()[0] == 1


def test_prune_dry_run_counts_without_deleting(db: sqlite3.Connection) -> None:
    _raw(db, keep_until=now_ms() - 1_000)
    assert prune_expired_raw(db, dry_run=True) == 1
    assert db.execute("SELECT count(*) FROM raw_api_response").fetchone()[0] == 1


def test_integrity_check_passes_on_healthy_db(db: sqlite3.Connection) -> None:
    ok, detail = integrity_check(db)
    assert ok and detail == "ok"


def test_database_size_includes_wal(db: sqlite3.Connection) -> None:
    from spotify_taste.config import get_settings

    assert database_size(get_settings().db_path) > 0


# --- 리포트 ---------------------------------------------------------------


def test_report_counts_sessions_by_source(db: sqlite3.Connection) -> None:
    _session(db, track_uri="spotify:track:A")
    _session(db, track_uri="spotify:track:B", source="recently_played", timing_confidence="low")
    report = build_daily_report(db, window_days=1)
    assert (report.poller_sessions, report.rp_sessions) == (1, 1)


def test_report_warns_when_nothing_collected(db: sqlite3.Connection) -> None:
    report = build_daily_report(db)
    assert not report.healthy
    assert "heartbeat-check" in report.warnings[0]


def _sample(
    db: sqlite3.Connection,
    *,
    http_status: int = 204,
    is_playing: int | None = None,
    sampled_at: int | None = None,
) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO player_sample (sampled_at, http_status, is_playing) VALUES (?, ?, ?)",
            (sampled_at or now_ms() - 60_000, http_status, is_playing),
        )


def test_report_quiet_day_is_note_not_warning(db: sqlite3.Connection) -> None:
    """폴러가 온종일 돌았지만 재생이 없던 날은 장애가 아니다.
    2026-08-13 실제 오탐: 204 샘플 723개에 세션 0으로 헛경보가 났다."""
    for i in range(3):
        _sample(db, sampled_at=now_ms() - i * 120_000)
    report = build_daily_report(db)
    assert report.healthy
    assert any("수집기는 정상" in n for n in report.notes)


def test_report_still_warns_when_playing_but_no_sessions(db: sqlite3.Connection) -> None:
    """재생 중 샘플이 있는데 세션이 하나도 없으면 그건 진짜 문제다."""
    _sample(db, http_status=200, is_playing=1)
    report = build_daily_report(db)
    assert not report.healthy
    assert "heartbeat-check" in report.warnings[0]


def test_report_warns_on_high_null_context(db: sqlite3.Connection) -> None:
    for i in range(10):
        _session(db, track_uri=f"spotify:track:{i}", started_at=now_ms() - i * 1_000)
    report = build_daily_report(db)
    assert any("CONTEXT_NULL_ACCOUNT" in w for w in report.warnings)


def test_report_warns_when_only_rp_sessions(db: sqlite3.Connection) -> None:
    """폴러 세션이 0 이면 ms_played 실측이 전혀 없다."""
    _session(
        db,
        track_uri="spotify:track:A",
        source="recently_played",
        timing_confidence="low",
        context_uri="spotify:playlist:P",
        context_origin="self",
    )
    report = build_daily_report(db)
    assert any("폴러 세션이 0" in w for w in report.warnings)


def test_report_counts_gap_minutes(db: sqlite3.Connection) -> None:
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) VALUES (?, ?, 'error_backoff')",
            (now_ms() - 600_000, now_ms() - 300_000),
        )
    report = build_daily_report(db)
    assert report.gap_minutes == 5.0


def test_report_cli_exits_nonzero_when_unhealthy(db: sqlite3.Connection) -> None:
    """launchd 잡이 종료 코드로 문제를 알아챈다."""
    result = runner.invoke(app, ["ops", "report"])
    assert result.exit_code == 1


def test_report_cli_ok_when_healthy(db: sqlite3.Connection) -> None:
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    result = runner.invoke(app, ["ops", "report"])
    assert result.exit_code == 0
    assert "세션" in result.output


def test_null_context_ratio_ignores_history_export(db: sqlite3.Connection) -> None:
    """export 는 구조적으로 context 가 없다 — 섞으면 '계정 증상' 경고가
    오탐을 내고, 그걸 믿고 CONTEXT_NULL_ACCOUNT 를 켜면 라이브 데이터
    분류까지 망가진다. 실데이터 39k 임포트 직후 실제로 겪었다."""
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    for i in range(20):
        _session(
            db,
            track_uri=f"spotify:track:E{i}",
            started_at=now_ms() - 1_000 - i,
            source="history_export",
            context_origin="self",
        )
    report = build_daily_report(db)
    assert report.null_context_ratio == 0.0
    assert not any("CONTEXT_NULL_ACCOUNT" in w for w in report.warnings)
