"""행동 프로파일 (3층).

이 파일이 지키는 것: **분모에서 '모름'을 뺀다**, **'처음 듣는 곡'은 필터보다
먼저 정해진다**, 그리고 분모가 0 인 지표는 0.0 이 아니라 **행이 없다**.
"""

from __future__ import annotations

import sqlite3

import pytest

from spotify_taste._common import now_ms
from spotify_taste.db.connection import tx
from spotify_taste.features import (
    BehaviorInput,
    build_behavior_profile,
    build_features,
    compute_behavior_profile,
    create_params_version,
)

DAY = 86_400_000


def _row(**over) -> BehaviorInput:
    kwargs = {
        "track_uri": "spotify:track:T1",
        "completion": 0.9,
        "completion_source": "measured",
        "context_origin": None,
        "hour": 12,
        "dow": 3,
    }
    kwargs.update(over)
    return BehaviorInput(**kwargs)


def _profile(rows, *, threshold: float = 0.05) -> dict[tuple[str, str, str], tuple[float, int]]:
    metrics = compute_behavior_profile(rows, abandon_threshold=threshold)
    return {(m.period, m.metric, m.source_filter): (m.value, m.sample_size) for m in metrics}


# --- 분모 (규칙: 모르는 것은 세지 않는다) ----------------------------------


def test_unknown_completion_is_not_counted_as_finished() -> None:
    """완주도를 모르는 재생을 '안 끊었다'로 세면 백필이 덜 된 만큼
    스킵률이 낮게 나온다."""
    prof = _profile(
        [
            _row(completion=0.01),  # 이탈
            _row(completion=None, completion_source="none"),
            _row(completion=None, completion_source="none"),
        ]
    )
    value, sample = prof[("all", "skip_rate", "all")]
    assert value == 1.0  # 완주도를 아는 1건이 전부 이탈
    assert sample == 1  # 분모는 3 이 아니라 1


def test_unknown_origin_is_out_of_the_voluntary_denominator() -> None:
    """unknown 을 '자발이 아님'으로 세면 null context 를 algorithmic 으로
    추측하는 것과 같아진다."""
    prof = _profile(
        [
            _row(context_origin="self"),
            _row(context_origin="algorithmic"),
            _row(context_origin=None),
            _row(context_origin="external"),
        ]
    )
    assert prof[("all", "voluntary_rate", "all")] == (0.5, 2)
    # 분류가 얼마나 됐는지도 함께 나와야 위 값을 얼마나 믿을지 알 수 있다.
    assert prof[("all", "classified_rate", "all")] == (0.5, 4)


def test_metric_with_empty_denominator_is_absent_not_zero() -> None:
    """0.0 으로 채우면 '없음'과 '0 이다'를 구별할 수 없다."""
    prof = _profile([_row(context_origin=None), _row(context_origin=None)])
    assert ("all", "voluntary_rate", "all") not in prof
    assert prof[("all", "classified_rate", "all")] == (0.0, 2)


def test_all_completions_unknown_leaves_no_completion_metrics() -> None:
    prof = _profile([_row(completion=None, completion_source="none")])
    assert ("all", "completion_mean", "all") not in prof
    assert ("all", "skip_rate", "all") not in prof
    assert prof[("all", "play_count", "all")] == (1.0, 1)


# --- 처음 듣는 곡 ----------------------------------------------------------


def test_first_play_is_decided_before_the_source_filter() -> None:
    """`measured` 안에서 처음을 찾으면 '실측된 것 중 처음'이 되어 뜻이 달라진다.

    같은 곡을 추정으로 먼저 듣고 나중에 실측으로 들었다면, 그 실측 재생은
    탐색이 아니다.
    """
    rows = [
        _row(track_uri="A", completion_source="inferred", completion=0.5),
        _row(track_uri="A", completion_source="measured"),
    ]
    prof = _profile(rows)
    assert prof[("all", "exploration_rate", "all")] == (0.5, 2)
    # measured 벌에는 1건뿐이고 그건 첫 재생이 아니다.
    assert prof[("all", "exploration_rate", "measured")] == (0.0, 1)


def test_exploration_counts_each_track_once() -> None:
    prof = _profile([_row(track_uri=u) for u in ("A", "B", "A", "C", "A")])
    assert prof[("all", "exploration_rate", "all")] == (3 / 5, 5)
    assert prof[("all", "repeat_rate", "all")] == (1 - 3 / 5, 5)


def test_repeat_rate_is_zero_when_every_track_differs() -> None:
    prof = _profile([_row(track_uri=u) for u in ("A", "B", "C")])
    assert prof[("all", "repeat_rate", "all")] == (0.0, 3)


# --- 기간 ------------------------------------------------------------------


def test_hour_and_dow_buckets_split_the_same_plays() -> None:
    rows = [_row(hour=3, dow=0), _row(hour=3, dow=1), _row(hour=21, dow=1)]
    prof = _profile(rows)
    assert prof[("hour:03", "play_count", "all")] == (2.0, 2)
    assert prof[("hour:21", "play_count", "all")] == (1.0, 1)
    assert prof[("dow:1", "play_count", "all")] == (2.0, 2)
    # 몫은 전체 재생 대비다 — 시간대끼리 합하면 1 이 된다.
    assert prof[("hour:03", "play_share", "all")][0] == pytest.approx(2 / 3)
    assert prof[("hour:21", "play_share", "all")][0] == pytest.approx(1 / 3)


def test_hour_is_zero_padded_so_periods_sort() -> None:
    prof = _profile([_row(hour=3), _row(hour=13)])
    assert ("hour:03", "play_count", "all") in prof
    assert ("hour:13", "play_count", "all") in prof


def test_all_period_has_no_share() -> None:
    """전 구간의 몫은 언제나 1 이라 아무것도 말해 주지 않는다."""
    prof = _profile([_row()])
    assert ("all", "play_share", "all") not in prof


# --- 분포 ------------------------------------------------------------------


def test_percentiles_return_observed_values_not_interpolations() -> None:
    """보간하면 실제로 관측되지 않은 완주도가 지표로 올라온다."""
    rows = [_row(completion=c) for c in (0.0, 0.5, 1.0)]
    prof = _profile(rows)
    for metric in ("completion_p25", "completion_p50", "completion_p75"):
        assert prof[("all", metric, "all")][0] in (0.0, 0.5, 1.0)
    assert prof[("all", "completion_p50", "all")][0] == 0.5


def test_abandon_threshold_comes_from_the_params() -> None:
    rows = [_row(completion=0.2)]
    assert _profile(rows, threshold=0.05)[("all", "skip_rate", "all")][0] == 0.0
    assert _profile(rows, threshold=0.30)[("all", "skip_rate", "all")][0] == 1.0


# --- 드라이버 --------------------------------------------------------------


def _seed(db: sqlite3.Connection) -> None:
    """0층까지 적재된 DB 를 만든다. 3층은 그 위에서만 돈다."""
    now = now_ms()
    with tx(db):
        db.execute(
            "INSERT INTO track (uri, name, duration_ms) VALUES (?, ?, ?)",
            ("spotify:track:T1", "곡", 200_000),
        )
        for i in range(3):
            db.execute(
                "INSERT INTO play_session"
                " (track_uri, started_at, ended_at, ms_played, source, timing_confidence,"
                "  context_origin, is_open, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'history_export', 'high', 'self', 0, ?, ?)",
                ("spotify:track:T1", now - (3 - i) * DAY, now, 180_000, now, now),
            )
    create_params_version(db)
    build_features(db, write=True)


def test_behavior_is_dry_run_by_default(db: sqlite3.Connection) -> None:
    _seed(db)
    result = build_behavior_profile(db)
    assert result.metrics  # 계산은 했다
    assert db.execute("SELECT count(*) FROM behavior_profile").fetchone()[0] == 0


def test_behavior_writes_rows_with_the_params_version(db: sqlite3.Connection) -> None:
    _seed(db)
    result = build_behavior_profile(db, write=True)
    rows = db.execute("SELECT DISTINCT params_version FROM behavior_profile").fetchall()
    assert [r[0] for r in rows] == [result.params_version]
    assert db.execute("SELECT count(*) FROM behavior_profile").fetchone()[0] == len(result.metrics)


def test_rebuild_does_not_leave_stale_rows(db: sqlite3.Connection) -> None:
    """지표 정의가 빠지거나 기간이 사라져도 낡은 값이 남으면 안 된다."""
    _seed(db)
    build_behavior_profile(db, write=True)
    with tx(db):
        db.execute(
            "INSERT INTO behavior_profile"
            " (period, metric, source_filter, params_version, value, sample_size, computed_at)"
            " VALUES ('hour:04', 'ghost_metric', 'all', 1, 0.5, 10, 0)"
        )
    build_behavior_profile(db, write=True)
    ghost = db.execute(
        "SELECT count(*) FROM behavior_profile WHERE metric = 'ghost_metric'"
    ).fetchone()[0]
    assert ghost == 0


def test_missing_layer_zero_is_reported_not_silently_empty(db: sqlite3.Connection) -> None:
    """0 이라는 답과 '아직 안 돌았다'는 구별돼야 한다."""
    create_params_version(db)
    with pytest.raises(LookupError, match="0층"):
        build_behavior_profile(db)


def test_driver_reads_kst_hours(db: sqlite3.Connection) -> None:
    """UTC 로 세면 시간대 프로파일이 9시간 밀린다."""
    now = now_ms()
    # 2026-08-23 00:30 KST = 2026-08-22 15:30 UTC
    started = 1787412600000
    with tx(db):
        db.execute(
            "INSERT INTO track (uri, name, duration_ms) VALUES (?, ?, ?)",
            ("spotify:track:T1", "곡", 200_000),
        )
        db.execute(
            "INSERT INTO play_session"
            " (track_uri, started_at, ended_at, ms_played, source, timing_confidence,"
            "  is_open, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'history_export', 'high', 0, ?, ?)",
            ("spotify:track:T1", started, started + 180_000, 180_000, now, now),
        )
    create_params_version(db)
    build_features(db, write=True)

    result = build_behavior_profile(db)
    periods = {m.period for m in result.metrics}
    assert "hour:00" in periods
    assert "hour:15" not in periods
