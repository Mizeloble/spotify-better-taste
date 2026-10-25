"""Dead-man switch.

핵심: **살아 있음 = "수집했다"이지 "루프를 돌았다"가 아니다.** 토큰이 죽은
채 도는 프로세스가 heartbeat 를 영원히 쓰는 상황을 잡는 것이 목적이다.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spotify_taste.cli import app
from spotify_taste.config import get_settings
from spotify_taste.db.connection import tx
from spotify_taste.heartbeat import (
    DEFAULT_MAX_AGE_SEC,
    Heartbeat,
    HeartbeatStatus,
    check_heartbeat,
)

runner = CliRunner()
NOW = 1_800_000_000_000


def _age(path: Path, seconds: float) -> None:
    """파일 mtime 을 과거로 돌린다 (실제로 기다릴 수는 없다)."""
    stamp = path.stat().st_mtime - seconds
    os.utime(path, (stamp, stamp))


# --- 순수 판정 ------------------------------------------------------------


def test_missing_file_is_reported(tmp_path: Path) -> None:
    verdict = check_heartbeat(path=tmp_path / "none.txt", now_ms_value=NOW)
    assert verdict.status is HeartbeatStatus.MISSING
    assert verdict.should_alert


def test_fresh_heartbeat_is_ok(tmp_path: Path) -> None:
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    verdict = check_heartbeat(path=hb_path, now_ms_value=int(os.path.getmtime(hb_path) * 1000))
    assert verdict.status is HeartbeatStatus.OK
    assert not verdict.should_alert


def test_stale_heartbeat_alerts(tmp_path: Path) -> None:
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    _age(hb_path, DEFAULT_MAX_AGE_SEC + 60)
    import time

    verdict = check_heartbeat(path=hb_path, now_ms_value=int(time.time() * 1000))
    assert verdict.status is HeartbeatStatus.STALE
    assert verdict.should_alert


def test_shutdown_sentinel_suppresses_alert(tmp_path: Path) -> None:
    """사람이 일부러 멈춘 것까지 알람으로 만들면 알람을 안 보게 된다."""
    hb_path = tmp_path / "hb.txt"
    sentinel = tmp_path / "stopped.txt"
    Heartbeat(hb_path).write()
    _age(hb_path, DEFAULT_MAX_AGE_SEC * 10)
    sentinel.write_text("stopped")

    import time

    verdict = check_heartbeat(
        path=hb_path, now_ms_value=int(time.time() * 1000), stopped_sentinel=sentinel
    )
    assert verdict.status is HeartbeatStatus.STOPPED
    assert not verdict.should_alert


def test_fresh_file_but_stale_data_still_alerts(tmp_path: Path) -> None:
    """이 프로젝트에서 heartbeat 를 데이터 기준으로도 보는 이유 전체 (C7).

    토큰이 죽었거나 DB 락에 걸린 프로세스도 루프는 계속 돈다.
    """
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    import time

    now = int(time.time() * 1000)
    verdict = check_heartbeat(
        path=hb_path,
        now_ms_value=now,
        last_sample_at=now - (DEFAULT_MAX_AGE_SEC + 600) * 1000,
    )
    assert verdict.status is HeartbeatStatus.STALE
    assert "마지막 샘플" in verdict.detail


def test_fresh_file_and_fresh_data_is_ok(tmp_path: Path) -> None:
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    import time

    now = int(time.time() * 1000)
    verdict = check_heartbeat(path=hb_path, now_ms_value=now, last_sample_at=now - 30_000)
    assert verdict.status is HeartbeatStatus.OK


# --- Mac 슬립 (2026-10-18) -------------------------------------------------
#
# 슬립을 허용하는 운영에서는 깨어날 때마다 heartbeat 가 수십 분 낡아 있다.
# 10-05~10-17 에 하루 ~10건씩, 총 130건의 헛경보가 정확히 이 모양이었다.


def test_just_woke_up_is_not_stale(tmp_path: Path) -> None:
    """40분 자고 1분 전에 깼다 — 수집기가 따라잡을 시간이 아직 있다."""
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    _age(hb_path, 40 * 60)
    now = int(os.path.getmtime(hb_path) * 1000) + 40 * 60_000
    verdict = check_heartbeat(
        path=hb_path,
        now_ms_value=now,
        last_sample_at=now - 40 * 60_000,
        last_wake_ms=now - 60_000,
    )
    assert verdict.status is HeartbeatStatus.OK
    assert not verdict.should_alert


def test_awake_long_enough_but_silent_still_alerts(tmp_path: Path) -> None:
    """깨어난 지 max_age 를 넘겼는데도 못 썼다면 그건 수집기 문제다."""
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    _age(hb_path, 3 * 3600)
    now = int(os.path.getmtime(hb_path) * 1000) + 3 * 3_600_000
    verdict = check_heartbeat(
        path=hb_path,
        now_ms_value=now,
        last_wake_ms=now - (DEFAULT_MAX_AGE_SEC + 60) * 1000,
    )
    assert verdict.status is HeartbeatStatus.STALE
    assert "깨어난 지 16분" in verdict.detail


def test_woke_up_but_data_stale_past_grace_alerts(tmp_path: Path) -> None:
    """파일은 신선해도 데이터 검사(C7)에 같은 유예가 적용된다 — 유예가 끝나면 알린다."""
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    now = int(os.path.getmtime(hb_path) * 1000)
    verdict = check_heartbeat(
        path=hb_path,
        now_ms_value=now,
        last_sample_at=now - 2 * 3_600_000,
        last_wake_ms=now - (DEFAULT_MAX_AGE_SEC + 60) * 1000,
    )
    assert verdict.status is HeartbeatStatus.STALE
    assert "마지막 샘플" in verdict.detail


def test_future_wake_time_is_ignored(tmp_path: Path) -> None:
    """시계가 꼬여 기상 시각이 '지금' 보다 뒤면 믿지 않는다 — 알람을 영원히 막는다."""
    hb_path = tmp_path / "hb.txt"
    Heartbeat(hb_path).write()
    _age(hb_path, DEFAULT_MAX_AGE_SEC + 60)
    now = int(os.path.getmtime(hb_path) * 1000) + (DEFAULT_MAX_AGE_SEC + 60) * 1000
    verdict = check_heartbeat(path=hb_path, now_ms_value=now, last_wake_ms=now + 3_600_000)
    assert verdict.status is HeartbeatStatus.STALE


def test_read_last_wake_ms_parses_sysctl(monkeypatch: pytest.MonkeyPatch) -> None:
    from spotify_taste import heartbeat

    class _Done:
        stdout = "{ sec = 1792333890, usec = 840295 } Sun Oct 18 23:31:30 2026\n"

    monkeypatch.setattr(heartbeat.sys, "platform", "darwin")
    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: _Done())
    assert heartbeat.read_last_wake_ms() == 1_792_333_890_000


def test_read_last_wake_ms_never_slept_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from spotify_taste import heartbeat

    class _Done:
        stdout = "{ sec = 0, usec = 0 } Thu Jan  1 09:00:00 1970\n"

    monkeypatch.setattr(heartbeat.sys, "platform", "darwin")
    monkeypatch.setattr(heartbeat.subprocess, "run", lambda *a, **k: _Done())
    assert heartbeat.read_last_wake_ms() is None


def test_read_last_wake_ms_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """sysctl 이 실패해도 판정은 막히지 않는다 — 예전처럼 슬립을 구분하지 않을 뿐."""
    from spotify_taste import heartbeat

    def boom(*a: object, **k: object) -> None:
        raise OSError("no sysctl")

    monkeypatch.setattr(heartbeat.sys, "platform", "darwin")
    monkeypatch.setattr(heartbeat.subprocess, "run", boom)
    assert heartbeat.read_last_wake_ms() is None


def test_read_last_wake_ms_off_macos_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from spotify_taste import heartbeat

    monkeypatch.setattr(heartbeat.sys, "platform", "linux")
    assert heartbeat.read_last_wake_ms() is None


# --- 쓰기 -----------------------------------------------------------------


def test_write_creates_parent_directory(tmp_path: Path) -> None:
    hb_path = tmp_path / "deep" / "hb.txt"
    Heartbeat(hb_path).write()
    assert hb_path.exists()


def test_write_failure_does_not_raise(tmp_path: Path) -> None:
    """heartbeat 를 못 쓴다고 수집이 멈추면 본말이 전도된다."""
    blocker = tmp_path / "blocked"
    blocker.write_text("나는 파일이다")
    Heartbeat(blocker / "hb.txt").write()  # 예외가 나면 안 된다


def test_sentinel_lifecycle(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "hb.txt")
    sentinel = tmp_path / "stopped.txt"
    hb.mark_stopped(sentinel)
    assert sentinel.exists()
    hb.clear_stopped(sentinel)
    assert not sentinel.exists()


# --- CLI ------------------------------------------------------------------


def test_heartbeat_check_exits_nonzero_when_dead(db: sqlite3.Connection) -> None:
    """launchd 잡이 종료 코드를 게이트로 쓴다."""
    result = runner.invoke(app, ["ops", "heartbeat-check"])
    assert result.exit_code == 1
    assert "missing" in result.output


def test_heartbeat_check_ok_when_fresh(db: sqlite3.Connection) -> None:
    settings = get_settings()
    Heartbeat(settings.heartbeat_path).write()
    with tx(db):
        db.execute(
            "INSERT INTO player_sample (sampled_at, http_status) VALUES (?, 204)",
            (int(__import__("time").time() * 1000),),
        )
    result = runner.invoke(app, ["ops", "heartbeat-check"])
    assert result.exit_code == 0
    assert "정상" in result.output


def test_heartbeat_check_quiet_prints_nothing_when_ok(db: sqlite3.Connection) -> None:
    settings = get_settings()
    Heartbeat(settings.heartbeat_path).write()
    result = runner.invoke(app, ["ops", "heartbeat-check", "--quiet"])
    assert result.exit_code == 0
    assert result.output.strip() == ""


def test_heartbeat_check_respects_shutdown_sentinel(db: sqlite3.Connection) -> None:
    settings = get_settings()
    Heartbeat(settings.heartbeat_path).write()
    _age(settings.heartbeat_path, DEFAULT_MAX_AGE_SEC * 10)
    settings.shutdown_sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    settings.shutdown_sentinel_path.write_text("stopped")

    result = runner.invoke(app, ["ops", "heartbeat-check"])
    assert result.exit_code == 0


def test_heartbeat_check_stays_quiet_right_after_wake(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DarkWake 몇 초 사이에 밀린 점검이 돌아도 알리지 않는다."""
    import time

    from spotify_taste.cli import _ops

    settings = get_settings()
    Heartbeat(settings.heartbeat_path).write()
    _age(settings.heartbeat_path, 45 * 60)
    monkeypatch.setattr(_ops, "read_last_wake_ms", lambda: int(time.time() * 1000) - 30_000)
    result = runner.invoke(app, ["ops", "heartbeat-check"])
    assert result.exit_code == 0


def test_heartbeat_check_alerts_when_awake_and_stale(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spotify_taste.cli import _ops

    settings = get_settings()
    Heartbeat(settings.heartbeat_path).write()
    _age(settings.heartbeat_path, 45 * 60)
    monkeypatch.setattr(_ops, "read_last_wake_ms", lambda: None)
    result = runner.invoke(app, ["ops", "heartbeat-check"])
    assert result.exit_code == 1
    assert "stale" in result.output


def test_preflight_reports_every_missing_precondition(db: sqlite3.Connection) -> None:
    """막힌 것을 하나씩 알려 주는 것보다 한 번에 보여 주는 편이 낫다."""
    result = runner.invoke(app, ["ops", "preflight"])
    assert result.exit_code == 1
    assert "토큰이 없습니다" in result.output
