"""Extended Streaming History 임포터.

지키는 불변식: **ts 는 종료 시각이다**(started_at 은 역산), **멱등이다**,
**폴러가 이기고 RP 는 대체된다**, **비율을 지어내지 않는다**.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spotify_taste._common import iso_to_epoch_ms
from spotify_taste.db.connection import tx
from spotify_taste.importer.extended_history import (
    find_audio_files,
    import_extended_history,
)

A = "spotify:track:AAAA"
TS = "2023-05-01T12:00:00Z"
TS_MS = iso_to_epoch_ms(TS)


def _record(**over) -> dict:
    base = {
        "ts": TS,
        "platform": "android",
        "ms_played": 180_000,
        "conn_country": "KR",
        "master_metadata_track_name": "곡",
        "master_metadata_album_artist_name": "아티스트",
        "spotify_track_uri": A,
        "episode_name": None,
        "reason_start": "clickrow",
        "reason_end": "trackdone",
        "shuffle": False,
        "skipped": False,
        "offline": False,
        "incognito_mode": False,
    }
    base.update(over)
    return base


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    d = tmp_path / "export" / "Spotify Extended Streaming History"
    d.mkdir(parents=True)
    return d


def _write(export_dir: Path, records: list[dict], *, year: int = 2023) -> None:
    (export_dir / f"Streaming_History_Audio_{year}.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )


def _run(db: sqlite3.Connection, export_dir: Path, *, write: bool = True):
    if write:
        with tx(db):
            return import_extended_history(db, export_dir.parent, write=True)
    return import_extended_history(db, export_dir.parent, write=False)


# --- 시각 역산 (가장 중요한 규칙) -------------------------------------------


def test_started_at_is_ts_minus_ms_played(db: sqlite3.Connection, export_dir: Path) -> None:
    """ts 는 재생이 끝난 시각이다. 이걸 시작으로 쓰면 모든 세션이 곡 길이만큼
    밀리고 시간대 프로필이 통째로 어긋난다."""
    _write(export_dir, [_record(ms_played=180_000)])
    _run(db, export_dir)
    row = db.execute("SELECT started_at, ended_at, ms_played FROM play_session").fetchone()
    assert row["ended_at"] == TS_MS
    assert row["started_at"] == TS_MS - 180_000


def test_measured_ms_played_is_stored(db: sqlite3.Connection, export_dir: Path) -> None:
    """export 의 ms_played 는 Spotify 실측이다 — RP 와 달리 채워도 된다."""
    _write(export_dir, [_record(ms_played=42_000)])
    _run(db, export_dir)
    row = db.execute("SELECT ms_played, source, timing_confidence FROM play_session").fetchone()
    assert (row["ms_played"], row["source"]) == (42_000, "history_export")
    assert row["timing_confidence"] == "high"


def test_ratios_are_not_fabricated(db: sqlite3.Connection, export_dir: Path) -> None:
    """export 에는 곡 길이가 없다. 비율은 백필 후 조인으로 계산한다."""
    _write(export_dir, [_record()])
    _run(db, export_dir)
    row = db.execute("SELECT completion_ratio, listened_ratio FROM play_session").fetchone()
    assert row["completion_ratio"] is None
    assert row["listened_ratio"] is None


def test_spotify_verdicts_preserved_verbatim(db: sqlite3.Connection, export_dir: Path) -> None:
    """reason_*/skipped 는 Spotify 자체 판정이다 — 원문 그대로 남긴다."""
    _write(export_dir, [_record(reason_start="fwdbtn", reason_end="fwdbtn", skipped=True)])
    _run(db, export_dir)
    row = db.execute("SELECT reason_start, end_reason, skipped FROM play_session").fetchone()
    assert (row["reason_start"], row["end_reason"], row["skipped"]) == ("fwdbtn", "fwdbtn", 1)


def test_platform_and_flags_mapped(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, [_record(platform="ios", shuffle=True, incognito_mode=True)])
    _run(db, export_dir)
    row = db.execute("SELECT device_type, shuffle, device_is_private FROM play_session").fetchone()
    assert (row["device_type"], row["shuffle"], row["device_is_private"]) == ("ios", 1, 1)


# --- 거르기 -----------------------------------------------------------------


def test_podcasts_and_audiobooks_skipped(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(
        export_dir,
        [_record(spotify_track_uri=None), _record()],
    )
    stats = _run(db, export_dir)
    assert stats.skipped_not_music == 1
    assert stats.imported == 1


def test_zero_ms_rows_are_queue_artifacts(db: sqlite3.Connection, export_dir: Path) -> None:
    """0ms 는 재생이 아니다. 스킵 신호는 짧아도 ms_played > 0 으로 남는다."""
    _write(export_dir, [_record(ms_played=0), _record(ms_played=800)])
    stats = _run(db, export_dir)
    assert stats.skipped_zero_ms == 1
    assert stats.imported == 1


def test_video_files_are_not_picked_up(db: sqlite3.Connection, export_dir: Path) -> None:
    (export_dir / "Streaming_History_Video_2023.json").write_text(
        json.dumps([_record()]), encoding="utf-8"
    )
    assert find_audio_files(export_dir.parent) == []


def test_malformed_timestamp_skipped_not_fatal(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, [_record(ts="어제쯤"), _record()])
    stats = _run(db, export_dir)
    assert stats.imported == 1


# --- 멱등성과 겹침 -----------------------------------------------------------


def test_reimport_is_idempotent(db: sqlite3.Connection, export_dir: Path) -> None:
    """더 긴 export 가 오면 그냥 다시 돌리면 된다."""
    _write(export_dir, [_record()])
    _run(db, export_dir)
    stats = _run(db, export_dir)
    assert stats.duplicates == 1
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


def test_poller_session_wins_over_export(db: sqlite3.Connection, export_dir: Path) -> None:
    """폴러에는 진행 궤적까지 있다 — export 가 덮으면 안 된다."""
    started = TS_MS - 180_000
    with tx(db):
        db.execute(
            "INSERT INTO play_session (track_uri, started_at, ms_played, source, "
            "timing_confidence, created_at, updated_at) "
            "VALUES (?, ?, 170000, 'poller', 'high', 0, 0)",
            (A, started + 5_000),
        )
    _write(export_dir, [_record()])
    stats = _run(db, export_dir)
    assert stats.poller_wins == 1
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


def test_export_supersedes_rp_row(db: sqlite3.Connection, export_dir: Path) -> None:
    """RP 에는 ms_played 가 없고 export 에는 있다 — 지우지 않고 대체 표시."""
    started = TS_MS - 180_000
    with tx(db):
        db.execute(
            "INSERT INTO play_session (track_uri, started_at, source, "
            "timing_confidence, created_at, updated_at) "
            "VALUES (?, ?, 'recently_played', 'low', 0, 0)",
            (A, started + 30_000),  # 30초 반올림 오차
        )
    _write(export_dir, [_record()])
    stats = _run(db, export_dir)
    assert stats.superseded_rp == 1
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 2
    sources = [r[0] for r in db.execute("SELECT source FROM v_clean_session")]
    assert sources == ["history_export"]


# --- dry-run ----------------------------------------------------------------


def test_dry_run_writes_nothing(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, [_record()])
    stats = _run(db, export_dir, write=False)
    assert stats.imported == 1  # '임포트될' 개수
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 0


def test_stats_report_span_and_tracks(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(
        export_dir,
        [
            _record(),
            _record(ts="2021-01-01T00:03:00Z", spotify_track_uri="spotify:track:B"),
        ],
    )
    stats = _run(db, export_dir, write=False)
    assert len(stats.distinct_tracks) == 2
    assert stats.first_ts == iso_to_epoch_ms("2021-01-01T00:03:00Z") - 180_000
