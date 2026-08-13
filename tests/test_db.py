"""스키마·마이그레이션·핵심 제약.

여기서 검증하는 것들은 전부 "틀렸을 때 조용히 데이터가 썩는" 종류다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from spotify_taste._common import iso_to_epoch_ms
from spotify_taste.config import get_settings
from spotify_taste.db.connection import connect, tx
from spotify_taste.db.migrate import (
    current_version,
    discover,
    migrate,
    schema_fingerprint,
)


def _insert_session(conn: sqlite3.Connection, **overrides: object) -> int:
    """최소 필드만 채운 play_session 을 넣고 id 를 돌려준다."""
    row: dict[str, object] = {
        "track_uri": "spotify:track:A",
        "started_at": 1_700_000_000_000,
        "source": "poller",
        "timing_confidence": "high",
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_000_000,
    }
    row.update(overrides)
    cols = ", ".join(row)
    marks = ", ".join(["?"] * len(row))
    with tx(conn):
        cur = conn.execute(
            f"INSERT INTO play_session ({cols}) VALUES ({marks})", tuple(row.values())
        )
    return int(cur.lastrowid or 0)


# --- 마이그레이션 ---------------------------------------------------------


def test_migrate_from_empty_sets_user_version(db: sqlite3.Connection) -> None:
    assert current_version(db) == max(m.version for m in discover())


def test_migrate_is_idempotent(db: sqlite3.Connection) -> None:
    """launchd 래퍼가 매 실행마다 불러도 안전해야 한다."""
    before = schema_fingerprint(db)
    assert migrate(db) == []
    assert schema_fingerprint(db) == before


def test_migration_numbers_are_contiguous_and_unique() -> None:
    versions = [m.version for m in discover()]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


def test_failed_migration_leaves_nothing_behind(tmp_path: Path) -> None:
    """파일 하나 = 트랜잭션 하나. 스크립트 중간에서 깨지면 통째로 롤백돼야
    한다 — 절반만 적용된 스키마로 수집이 도는 것이 최악이다."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_broken.sql").write_text(
        "CREATE TABLE good (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;\n",
        encoding="utf-8",
    )
    conn = connect(tmp_path / "broken.db")
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate(conn, mig_dir)
        left = [r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")]
        assert left == []
        assert current_version(conn) == 0
    finally:
        conn.close()


def test_non_contiguous_migration_number_is_rejected(tmp_path: Path) -> None:
    """번호가 건너뛰면 어느 것이 적용됐는지 알 수 없게 된다."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0002_skipped.sql").write_text("CREATE TABLE t (id INTEGER);", encoding="utf-8")
    conn = connect(tmp_path / "gap.db")
    try:
        with pytest.raises(ValueError, match="이어지지 않습니다"):
            migrate(conn, mig_dir)
    finally:
        conn.close()


def test_wal_mode_is_persistent(db: sqlite3.Connection) -> None:
    """journal_mode 는 DB 파일 속성이라 새 커넥션에서도 유지돼야 한다."""
    path = get_settings().db_path
    other = connect(path)
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        other.close()


def test_foreign_keys_enforced(db: sqlite3.Connection) -> None:
    """track 이 없는 track_artist 는 들어가면 안 된다."""
    with pytest.raises(sqlite3.IntegrityError), tx(db):
        db.execute(
            "INSERT INTO track_artist (track_uri, artist_uri, position) VALUES (?, ?, 0)",
            ("spotify:track:nope", "spotify:artist:nope"),
        )


def test_readonly_connection_rejects_writes(db: sqlite3.Connection) -> None:
    ro = connect(get_settings().db_path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO sync_cursor (name, value, updated_at) VALUES ('x','y',0)")
    finally:
        ro.close()


def test_tx_rolls_back_on_exception(db: sqlite3.Connection) -> None:
    """한 틱은 통째로 성공하거나 통째로 실패해야 한다."""
    with pytest.raises(RuntimeError), tx(db):
        db.execute("INSERT INTO sync_cursor (name, value, updated_at) VALUES ('a','1',0)")
        raise RuntimeError("boom")
    assert db.execute("SELECT count(*) FROM sync_cursor").fetchone()[0] == 0


def test_tx_takes_the_write_lock_up_front(db: sqlite3.Connection) -> None:
    """`tx()` 는 읽기만 한 시점에도 이미 쓰기 락을 쥐고 있어야 한다.

    DEFERRED 로 열면 읽기→쓰기 업그레이드 때 `SQLITE_BUSY_SNAPSHOT` 이 나고,
    이건 busy 핸들러가 호출되지 않아 busy_timeout 을 통째로 무시한 채 즉시
    "database is locked" 가 된다. 2026-08-13 새벽 백필과 겹쳐 폴러 프로세스가
    그렇게 죽었다. 여기서는 대기 없이 판정하려고 상대의 busy_timeout 을 0 으로
    둔다 — 실제 운영에서는 5초를 기다린 뒤 성공한다.
    """
    other = connect(get_settings().db_path)
    other.execute("PRAGMA busy_timeout=0")
    try:
        with tx(db):
            # 폴러의 _persist 처럼 읽기로 시작한다 (raw_archive 가 모양을 먼저 본다).
            db.execute("SELECT count(*) FROM sync_cursor").fetchone()
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO sync_cursor (name, value, updated_at) VALUES ('me','1',0)")
    finally:
        other.close()
    assert db.execute("SELECT count(*) FROM sync_cursor").fetchone()[0] == 1


# --- play_session 제약 ----------------------------------------------------


def test_unique_track_and_start_time(db: sqlite3.Connection) -> None:
    _insert_session(db)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_session(db)


def test_upsert_updates_measurements_in_place(db: sqlite3.Connection) -> None:
    """폴러는 매 틱 같은 세션을 upsert 한다 — 행이 늘어나면 안 된다."""
    _insert_session(db, ms_played=1_000, is_open=1)
    with tx(db):
        db.execute(
            """
            INSERT INTO play_session
              (track_uri, started_at, source, timing_confidence,
               ms_played, is_open, created_at, updated_at)
            VALUES (?, ?, 'poller', 'high', ?, 0, ?, ?)
            ON CONFLICT (track_uri, started_at) DO UPDATE SET
              ms_played = excluded.ms_played,
              is_open   = excluded.is_open,
              updated_at = excluded.updated_at
            """,
            ("spotify:track:A", 1_700_000_000_000, 90_000, 1, 2),
        )
    rows = db.execute("SELECT ms_played, is_open FROM play_session").fetchall()
    assert len(rows) == 1
    assert (rows[0]["ms_played"], rows[0]["is_open"]) == (90_000, 0)


def test_replay_of_same_track_is_a_separate_row(db: sqlite3.Connection) -> None:
    """반복 재생은 started_at 이 달라 유니크 제약에 걸리지 않는다."""
    _insert_session(db, started_at=1_700_000_000_000)
    _insert_session(db, started_at=1_700_000_300_000)
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 2


def test_surrogate_id_is_stable_and_distinct(db: sqlite3.Connection) -> None:
    """복합 유니크 키와 별개의 대리키가 있어야 이후 조인이 병합을 견딘다."""
    a = _insert_session(db, started_at=1_700_000_000_000)
    b = _insert_session(db, started_at=1_700_000_300_000)
    assert a and b and a != b


def test_superseded_by_references_another_session(db: sqlite3.Connection) -> None:
    winner = _insert_session(db, started_at=1_700_000_000_000)
    loser = _insert_session(db, started_at=1_700_000_030_000, source="recently_played")
    with tx(db):
        db.execute("UPDATE play_session SET superseded_by = ? WHERE id = ?", (winner, loser))
    assert (
        db.execute("SELECT superseded_by FROM play_session WHERE id = ?", (loser,)).fetchone()[0]
        == winner
    )


# --- local_date 생성 컬럼 -------------------------------------------------


@pytest.mark.parametrize(
    ("utc_iso", "expected_kst_date"),
    [
        ("2026-08-09T12:00:00Z", "2026-08-09"),  # 낮 → 같은 날
        ("2026-08-09T14:59:59Z", "2026-08-09"),  # KST 23:59:59
        ("2026-08-09T15:00:00Z", "2026-08-10"),  # KST 자정 정각 → 다음 날
        ("2026-08-09T23:00:00Z", "2026-08-10"),  # KST 오전 8시
        ("2026-01-15T15:30:00Z", "2026-01-16"),  # 겨울에도 동일 (KST 는 DST 없음)
    ],
)
def test_local_date_uses_kst_boundary(
    db: sqlite3.Connection, utc_iso: str, expected_kst_date: str
) -> None:
    """새벽 청취가 전날로 집계되면 시간대 프로필이 통째로 어긋난다."""
    _insert_session(db, started_at=iso_to_epoch_ms(utc_iso))
    assert db.execute("SELECT local_date FROM play_session").fetchone()[0] == expected_kst_date


# --- v_clean_session 뷰 ---------------------------------------------------


def test_clean_view_excludes_superseded_and_phantom(db: sqlite3.Connection) -> None:
    """분석 쿼리가 제외 조건을 잊을 수 없게 만드는 것이 이 뷰의 존재 이유다."""
    keep = _insert_session(db, started_at=1_700_000_000_000)
    _insert_session(db, started_at=1_700_000_100_000, superseded_by=keep)
    _insert_session(db, started_at=1_700_000_200_000, suspect_phantom=1)

    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 3
    clean = db.execute("SELECT id FROM v_clean_session").fetchall()
    assert [r["id"] for r in clean] == [keep]


def test_clean_view_exposes_all_session_columns(db: sqlite3.Connection) -> None:
    """뷰가 컬럼을 빠뜨리면 분석 코드가 조용히 테이블 직접 조회로 돌아간다.

    `table_info` 는 생성 컬럼(local_date)을 숨기므로 `table_xinfo` 로 본다 —
    `SELECT *` 에는 생성 컬럼도 포함되고, 뷰는 그걸 그대로 물려받는다.
    """
    view_cols = {r[1] for r in db.execute("PRAGMA table_info(v_clean_session)")}
    table_cols = {r[1] for r in db.execute("PRAGMA table_xinfo(play_session)")}
    assert view_cols == table_cols
    assert "local_date" in view_cols


# --- Phase 2~6 테이블의 되돌릴 수 없는 설계 결정 --------------------------


def test_track_tag_pk_allows_retagging_without_overwrite(db: sqlite3.Connection) -> None:
    """model/prompt_version 이 PK 에 없으면 재태깅이 이력을 덮어써
    프롬프트 A/B 가 불가능해진다."""
    with tx(db):
        db.execute("INSERT INTO track (uri) VALUES ('spotify:track:A')")
        for model, version in (("claude-opus-5", 1), ("claude-opus-5", 2), ("other", 1)):
            db.execute(
                "INSERT INTO track_tag (track_uri, model, prompt_version, created_at) "
                "VALUES ('spotify:track:A', ?, ?, 0)",
                (model, version),
            )
    assert db.execute("SELECT count(*) FROM track_tag").fetchone()[0] == 3


def test_playlist_meta_defaults_to_not_generated(db: sqlite3.Connection) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO playlist_meta (uri, fetched_at, fetch_status, refresh_after) "
            "VALUES ('spotify:playlist:X', 0, 'ok', 0)"
        )
    assert db.execute("SELECT is_generated FROM playlist_meta").fetchone()[0] == 0


def test_recommendation_item_positions_are_unique_per_rec(db: sqlite3.Connection) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO recommendation (id, created_at, prompt_hash, model) "
            "VALUES (1, 0, 'h', 'm')"
        )
        db.execute(
            "INSERT INTO recommendation_item (rec_id, track_uri, position, slot_type) "
            "VALUES (1, 'spotify:track:A', 0, 'core')"
        )
    with pytest.raises(sqlite3.IntegrityError), tx(db):
        db.execute(
            "INSERT INTO recommendation_item (rec_id, track_uri, position, slot_type) "
            "VALUES (1, 'spotify:track:B', 0, 'core')"
        )
