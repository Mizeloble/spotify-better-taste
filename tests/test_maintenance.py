"""백업·정리·리포트.

지키는 불변식: **keep_until IS NULL 은 절대 지우지 않는다**(필드 삭제가
왔을 때 재구성할 유일한 근거이기 때문).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
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


def test_report_separates_unclassified_from_unknown(db: sqlite3.Connection) -> None:
    """'분류했는데 unknown' 과 '아직 분류 안 됨' 은 다른 문제다.
    04:40 배치 이후 생긴 세션이 unknown 100% 로 보이던 오해(2026-08-13)."""
    _session(
        db, track_uri="spotify:track:A", context_uri="spotify:playlist:P", context_origin="self"
    )
    _session(
        db, track_uri="spotify:track:B", context_uri="spotify:playlist:Q", context_origin="unknown"
    )
    _session(db, track_uri="spotify:track:C", context_uri="spotify:playlist:R")  # 미분류
    report = build_daily_report(db, window_days=1)
    assert report.unclassified == 1
    assert report.unknown_ratio == 0.5  # 분모는 분류된 2건
    assert any("미분류" in line for line in report.lines())


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


def test_poller_zero_while_asleep_is_note_not_warning(db: sqlite3.Connection) -> None:
    """Mac 이 잠든 날 RP 로만 들어오는 것은 정상 운영이다 (2026-10-18 결정).

    10-05~10-17 에 하루 ~1,420분을 자는 동안 이 경고가 수집기를 의심하게 만들었다.
    """
    _session(
        db,
        track_uri="spotify:track:A",
        source="recently_played",
        timing_confidence="low",
        context_uri="spotify:playlist:P",
        context_origin="self",
    )
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) VALUES (?, ?, 'host_sleep')",
            (now_ms() - 80_000_000, now_ms() - 1_000_000),
        )
    report = build_daily_report(db)
    assert report.healthy
    assert any("폴러 세션 0" in n and "다음 export" in n for n in report.notes)


def _export(db: sqlite3.Connection, *, ended_days_ago: float) -> None:
    ended = now_ms() - int(ended_days_ago * 86_400_000)
    _session(
        db,
        track_uri="spotify:track:X",
        source="history_export",
        started_at=ended - 200_000,
        ended_at=ended,
        ms_played=200_000,
    )


def test_report_shows_export_coverage_and_waiting_rp(db: sqlite3.Connection) -> None:
    _export(db, ended_days_ago=10)
    for i in range(3):
        _session(
            db,
            track_uri=f"spotify:track:R{i}",
            source="recently_played",
            timing_confidence="low",
            started_at=now_ms() - (i + 1) * 86_400_000,
        )
    # export 이전의 RP 는 이미 export 가 덮는 구간이라 '대기' 가 아니다.
    _session(
        db,
        track_uri="spotify:track:OLD",
        source="recently_played",
        timing_confidence="low",
        started_at=now_ms() - 20 * 86_400_000,
    )
    report = build_daily_report(db)
    assert report.rp_awaiting_export == 3
    assert any("완주 실측" in line and "RP 3건 대기" in line for line in report.lines())
    assert not any("Extended streaming history" in n for n in report.notes)


def test_stale_export_asks_for_a_new_request_as_note(db: sqlite3.Connection) -> None:
    """낡은 export 는 할 일이지만 장애는 아니다 — 기다리는 몇 주 동안 '주의' 가
    매일 뜨면 진짜 경고가 묻힌다."""
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    _export(db, ended_days_ago=70)
    report = build_daily_report(db)
    assert report.healthy
    note = next(n for n in report.notes if "Extended streaming history" in n)
    assert "70일 전" in note
    assert "import history --write" in note


def test_missing_export_is_noted_even_on_a_quiet_day(db: sqlite3.Connection) -> None:
    """듣지 않은 날이라고 export 가 덜 낡는 건 아니다."""
    report = build_daily_report(db)
    assert any("export 가 없습니다" in n for n in report.notes)


def test_report_counts_gap_minutes(db: sqlite3.Connection) -> None:
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) VALUES (?, ?, 'error_backoff')",
            (now_ms() - 600_000, now_ms() - 300_000),
        )
    report = build_daily_report(db)
    assert report.gap_minutes == 5.0
    assert report.gap_by_reason == {"error_backoff": 5.0}


def test_gap_that_started_before_the_window_is_counted_for_the_overlap(
    db: sqlite3.Connection,
) -> None:
    """창 밖에서 시작한 단절이 0분으로 사라지면 안 된다.

    2026-08-24~08-31 회귀 테스트. 7일짜리 공백을 `started_at >= since` 로
    거르면 리포트는 "수집 공백 0분" 이라고 답한다 — 가장 큰 사고를 가장
    조용히 넘기는 셈이다. 겹치는 구간(창 안 24시간)만 센다.
    """
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) "
            "VALUES (?, ?, 'process_restart')",
            (now_ms() - 7 * 86_400_000, now_ms()),
        )
    report = build_daily_report(db)
    assert report.gap_minutes == pytest.approx(1440.0, abs=1.0)
    assert any("수집 공백" in w for w in report.warnings)


def test_gap_warning_names_the_reasons(db: sqlite3.Connection) -> None:
    """합계만으로는 크래시인지 호스트 슬립인지 사람이 판단할 수 없다."""
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) "
            "VALUES (?, ?, 'process_restart')",
            (now_ms() - 50_000_000, now_ms() - 1_000_000),
        )
    report = build_daily_report(db)
    assert any("process_restart" in w for w in report.warnings)


def test_sleep_gap_is_excluded_from_the_verdict(db: sqlite3.Connection) -> None:
    """랩탑이 잠든 시간은 수집기 잘못이 아니다 (2026-08-14 결정).

    슬립 855분만으로는 healthy 여야 하고, 크래시 공백이 함께 있으면 그것만으로
    판정한다 — 경고 내역에도 슬립은 나오지 않는다.
    """
    _session(db, context_uri="spotify:playlist:P", context_origin="self")
    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) VALUES (?, ?, 'host_sleep')",
            (now_ms() - 55_000_000, now_ms() - 3_700_000),
        )
    report = build_daily_report(db)
    assert report.healthy
    assert any("호스트 슬립" in n for n in report.notes)

    with tx(db):
        db.execute(
            "INSERT INTO collector_gap (started_at, ended_at, reason) "
            "VALUES (?, ?, 'process_restart')",
            (now_ms() - 12_000_000, now_ms()),
        )
    report = build_daily_report(db)
    warning = next(w for w in report.warnings if "수집 공백" in w)
    assert "process_restart 200분" in warning
    assert "host_sleep" not in warning


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
