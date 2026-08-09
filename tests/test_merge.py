"""폴러 ↔ recently-played 병합.

지키는 불변식: **폴러가 항상 이긴다**, **실측을 날조하지 않는다**,
**행을 지우지 않는다**.
"""

from __future__ import annotations

import sqlite3

import pytest

from spotify_taste.db.connection import tx
from spotify_taste.sessions.merge import (
    CALIBRATION_KEY,
    DEDUP_WINDOW_MS,
    MATCH_WINDOW_MS,
    MIN_CALIBRATION_SAMPLES,
    calibrate_played_at_offset,
    get_offset_ms,
    infer_completion,
    merge_recent_plays,
    supersede_rp_with_poller,
)
from spotify_taste.spotify.schemas import RecentPlay

A = "spotify:track:AAAA"
B = "spotify:track:BBBB"
T0 = 1_800_000_000_000
THREE_MIN = 180_000


def _rp(
    track_uri: str, played_at: int, *, duration: int | None = THREE_MIN, ctx=None
) -> RecentPlay:
    return RecentPlay(
        track_uri=track_uri,
        played_at=played_at,
        duration_ms=duration,
        context_uri=ctx,
        context_type="playlist" if ctx else None,
    )


def _poller_session(db: sqlite3.Connection, *, track_uri=A, started_at=T0, **over) -> int:
    row: dict = {
        "track_uri": track_uri,
        "started_at": started_at,
        "ended_at": started_at + THREE_MIN,
        "ms_played": THREE_MIN,
        "duration_ms": THREE_MIN,
        "completion_ratio": 1.0,
        "listened_ratio": 1.0,
        "source": "poller",
        "timing_confidence": "high",
        "created_at": T0,
        "updated_at": T0,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(["?"] * len(row))
    with tx(db):
        cur = db.execute(f"INSERT INTO play_session ({cols}) VALUES ({marks})", tuple(row.values()))
    return int(cur.lastrowid or 0)


# --- 폴러가 이긴다 --------------------------------------------------------


def test_matching_poller_session_is_confirmed_not_duplicated(db: sqlite3.Connection) -> None:
    """같은 재생이 두 행으로 남으면 통계가 통째로 부풀려진다."""
    _poller_session(db)
    with tx(db):
        stats = merge_recent_plays(db, [_rp(A, T0 + 20_000)])  # 30초 반올림 오차
    assert stats.confirmed == 1 and stats.inserted == 0
    rows = db.execute("SELECT source, confirmed_by_rp FROM play_session").fetchall()
    assert len(rows) == 1
    assert (rows[0]["source"], rows[0]["confirmed_by_rp"]) == ("poller", 1)


def test_poller_measurements_are_never_overwritten(db: sqlite3.Connection) -> None:
    """RP 에는 ms_played 가 아예 없다. 덮어쓰면 실측을 잃는다."""
    _poller_session(db, ms_played=123_456)
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0 + 10_000)])
    assert db.execute("SELECT ms_played FROM play_session").fetchone()[0] == 123_456


def test_rp_backfills_missing_context_only(db: sqlite3.Connection) -> None:
    """폴러가 context 를 못 받은 계정에서 RP 가 메꿔 줄 수 있다."""
    _poller_session(db, context_uri=None, context_type=None)
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0, ctx="spotify:playlist:PL")])
    row = db.execute("SELECT context_uri, context_type FROM play_session").fetchone()
    assert (row["context_uri"], row["context_type"]) == ("spotify:playlist:PL", "playlist")


def test_existing_context_is_not_replaced(db: sqlite3.Connection) -> None:
    _poller_session(db, context_uri="spotify:album:ORIGINAL", context_type="album")
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0, ctx="spotify:playlist:OTHER")])
    assert (
        db.execute("SELECT context_uri FROM play_session").fetchone()[0] == "spotify:album:ORIGINAL"
    )


@pytest.mark.parametrize("skew_ms", [0, 30_000, 60_000, MATCH_WINDOW_MS])
def test_match_window_covers_rounding_and_poll_granularity(
    db: sqlite3.Connection, skew_ms: int
) -> None:
    """30초 반올림 + 25초 폴링 입도 + 시계 오차를 모두 덮어야 한다."""
    _poller_session(db)
    with tx(db):
        stats = merge_recent_plays(db, [_rp(A, T0 + skew_ms)])
    assert stats.confirmed == 1


def test_far_apart_play_is_a_separate_session(db: sqlite3.Connection) -> None:
    """진짜로 다시 튼 것을 같은 재생으로 합치면 반복 청취 신호가 사라진다."""
    _poller_session(db)
    with tx(db):
        stats = merge_recent_plays(db, [_rp(A, T0 + 30 * 60_000)])
    assert stats.confirmed == 0 and stats.inserted == 1


# --- 갭 보충 --------------------------------------------------------------


def test_gap_fill_marked_low_confidence_with_null_measurements(db: sqlite3.Connection) -> None:
    """추정을 실측 컬럼에 넣지 않는다 — 이 프로젝트의 핵심 규칙."""
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    row = db.execute("SELECT * FROM play_session").fetchone()
    assert row["source"] == "recently_played"
    assert row["timing_confidence"] == "low"
    assert row["ms_played"] is None
    assert row["completion_ratio"] is None
    assert row["listened_ratio"] is None
    assert row["end_reason"] == "rp_inferred"


def test_inferred_ratio_stored_in_its_own_column(db: sqlite3.Connection) -> None:
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0), _rp(B, T0 + THREE_MIN)])
    row = db.execute(
        "SELECT inferred_completion_ratio FROM play_session WHERE track_uri = ?", (A,)
    ).fetchone()
    assert row[0] == 1.0


def test_offset_shifts_inserted_sessions(db: sqlite3.Connection) -> None:
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)], offset_ms=-THREE_MIN)
    assert db.execute("SELECT started_at FROM play_session").fetchone()[0] == T0 - THREE_MIN


# --- 중복과 팬텀 ----------------------------------------------------------


def test_near_duplicate_rp_entry_skipped(db: sqlite3.Connection) -> None:
    """재생하지 않은 곡이 중복 항목으로 들어오는 사례가 보고돼 있다."""
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    with tx(db):
        stats = merge_recent_plays(db, [_rp(A, T0 + DEDUP_WINDOW_MS - 1_000)])
    assert stats.skipped_duplicate == 1
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


def test_exact_duplicate_absorbed_by_unique_constraint(db: sqlite3.Connection) -> None:
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


def test_later_poller_session_supersedes_rp_row(db: sqlite3.Connection) -> None:
    """폴러가 재시작 후 emit 했는데 RP 가 이미 그 재생을 기록해 둔 경우."""
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    poller_id = _poller_session(db, started_at=T0 + 10_000)

    with tx(db):
        n = supersede_rp_with_poller(
            db, poller_session_id=poller_id, track_uri=A, started_at=T0 + 10_000
        )
    assert n == 1
    # 지우지 않고 표시만 한다.
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM v_clean_session").fetchone()[0] == 1


def test_superseded_row_is_excluded_from_clean_view(db: sqlite3.Connection) -> None:
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    poller_id = _poller_session(db, started_at=T0 + 5_000)
    with tx(db):
        supersede_rp_with_poller(
            db, poller_session_id=poller_id, track_uri=A, started_at=T0 + 5_000
        )
    sources = [r[0] for r in db.execute("SELECT source FROM v_clean_session")]
    assert sources == ["poller"]


# --- infer_completion -----------------------------------------------------


def test_next_track_after_full_duration_means_completed() -> None:
    assert infer_completion(THREE_MIN, THREE_MIN) == 1.0


def test_longer_gap_than_duration_still_counts_as_completed() -> None:
    """트랙보다 길게 재생할 수는 없다 — 남는 시간은 곡 사이의 공백이다."""
    assert infer_completion(THREE_MIN + 55_000, THREE_MIN) == 1.0


def test_slightly_short_gap_still_counts_as_completed() -> None:
    """마지막 몇 초를 남기고 넘기는 것은 완주로 본다."""
    assert infer_completion(int(THREE_MIN * 0.95), THREE_MIN) == 1.0


def test_short_track_completed_when_gap_matches(db: sqlite3.Connection) -> None:
    """짧은 곡이라도 간격이 길이에 맞으면 완주다."""
    assert infer_completion(75_000, 80_000) == 1.0


def test_quick_next_track_means_skip() -> None:
    assert infer_completion(20_000, THREE_MIN) == pytest.approx(0.1111, abs=0.001)


def test_ambiguous_middle_band_returns_none() -> None:
    """30초 해상도에서 절반 청취는 복원 불가능하다. 아는 척하지 않는다."""
    assert infer_completion(90_000, THREE_MIN) is None


def test_missing_inputs_return_none() -> None:
    assert infer_completion(None, THREE_MIN) is None
    assert infer_completion(THREE_MIN, None) is None
    assert infer_completion(THREE_MIN, 0) is None


def test_negative_delta_returns_none() -> None:
    assert infer_completion(-1_000, THREE_MIN) is None


def test_short_track_quick_skip_not_inferred() -> None:
    """90초짜리 곡을 40초 만에 넘긴 것은 스킵인지 완주인지 애매하다."""
    assert infer_completion(40_000, 80_000) is None


def test_last_item_in_page_has_no_next_delta(db: sqlite3.Connection) -> None:
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0)])
    assert db.execute("SELECT inferred_completion_ratio FROM play_session").fetchone()[0] is None


def test_large_gap_between_items_is_not_a_next_track(db: sqlite3.Connection) -> None:
    """10분 넘게 벌어졌으면 '다음 곡'이 아니라 다른 청취 세션이다."""
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0), _rp(B, T0 + 20 * 60_000)])
    row = db.execute(
        "SELECT inferred_completion_ratio FROM play_session WHERE track_uri = ?", (A,)
    ).fetchone()
    assert row[0] is None


# --- 보정 -----------------------------------------------------------------


def test_calibration_detects_start_semantics(db: sqlite3.Connection) -> None:
    """겹친 구간에서 폴러의 started_at 이 정답이다."""
    for i in range(MIN_CALIBRATION_SAMPLES):
        at = T0 + i * 600_000
        _poller_session(db, track_uri=f"spotify:track:{i}", started_at=at)
        with tx(db):
            merge_recent_plays(db, [_rp(f"spotify:track:{i}", at + 300_000)])  # 창 밖 → 삽입

    median, count = calibrate_played_at_offset(db)
    assert count >= MIN_CALIBRATION_SAMPLES
    assert median == pytest.approx(300_000, abs=1_000)


def test_calibration_refuses_to_write_below_sample_floor(db: sqlite3.Connection) -> None:
    """표본이 적을 때 학습값을 믿으면 전체 타임라인이 어긋난다."""
    _poller_session(db)
    with tx(db):
        merge_recent_plays(db, [_rp(A, T0 + 200_000)])
    with tx(db):
        calibrate_played_at_offset(db, write=True)
    assert db.execute("SELECT count(*) FROM calibration").fetchone()[0] == 0


def test_offset_defaults_to_zero_without_calibration(db: sqlite3.Connection) -> None:
    assert get_offset_ms(db) == 0


def test_stored_offset_below_floor_is_ignored(db: sqlite3.Connection) -> None:
    from spotify_taste._common import now_ms
    from spotify_taste.db.queries import SET_CALIBRATION

    with tx(db):
        db.execute(SET_CALIBRATION, (CALIBRATION_KEY, "180000", 3, now_ms()))
    assert get_offset_ms(db) == 0


def test_stored_offset_above_floor_is_used(db: sqlite3.Connection) -> None:
    from spotify_taste._common import now_ms
    from spotify_taste.db.queries import SET_CALIBRATION

    with tx(db):
        db.execute(SET_CALIBRATION, (CALIBRATION_KEY, "180000", 999, now_ms()))
    assert get_offset_ms(db) == 180_000


# --- 순서 -----------------------------------------------------------------


def test_out_of_order_input_is_sorted(db: sqlite3.Connection) -> None:
    """API 는 최신순으로 주지만 병합은 시간순이어야 간격 추정이 맞는다."""
    with tx(db):
        merge_recent_plays(db, [_rp(B, T0 + THREE_MIN), _rp(A, T0)])
    row = db.execute(
        "SELECT inferred_completion_ratio FROM play_session WHERE track_uri = ?", (A,)
    ).fetchone()
    assert row[0] == 1.0


def test_empty_input_is_a_noop(db: sqlite3.Connection) -> None:
    with tx(db):
        stats = merge_recent_plays(db, [])
    assert (stats.confirmed, stats.inserted) == (0, 0)
