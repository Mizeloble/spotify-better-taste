"""취향 특성 0층·1층.

이 파일이 지키는 것: **모드는 가산점이 아니라 기저율 대비 정규화**,
**완주도 모름은 0 이 아니다**, **출처를 섞지 않는다**, 그리고 파라미터
버전이 모든 파생 행에 박힌다.
"""

from __future__ import annotations

import sqlite3

from spotify_taste._common import now_ms
from spotify_taste.db.connection import tx
from spotify_taste.features import (
    FeatureParams,
    build_features,
    compute_weight,
    create_params_version,
    load_params,
)
from spotify_taste.features.weights import resolve_completion

DAY = 86_400_000
PARAMS = FeatureParams(mode_base_rates={"trackdone": 0.84, "clickrow": 0.40}, global_base_rate=0.70)


def _weight(**over):
    kwargs = {
        "completion": 0.9,
        "completion_source": "export",
        "mode": "trackdone",
        "context_origin": None,
        "age_ms": 0,
        "params": PARAMS,
    }
    kwargs.update(over)
    return compute_weight(**kwargs)


# --- 완주도 출처 (규칙 3) --------------------------------------------------


def test_measured_beats_export_and_inferred() -> None:
    value, source = resolve_completion(
        measured=0.5, ms_played=180_000, duration_ms=200_000, inferred=0.9
    )
    assert (value, source) == (0.5, "measured")


def test_export_used_when_no_measurement() -> None:
    value, source = resolve_completion(
        measured=None, ms_played=100_000, duration_ms=200_000, inferred=0.9
    )
    assert (value, source) == (0.5, "export")


def test_completion_is_clamped_to_one() -> None:
    """한 export 행 안에서 반복 재생되면 1 을 넘는다 (실측 153건, 최대 4.3배)."""
    value, _ = resolve_completion(
        measured=None, ms_played=430_000, duration_ms=100_000, inferred=None
    )
    assert value == 1.0


def test_unknown_completion_is_not_zero() -> None:
    """백필이 덜 된 트랙. 0 으로 처리하면 롱테일을 통째로 싫어함으로 학습한다."""
    value, source = resolve_completion(
        measured=None, ms_played=100_000, duration_ms=None, inferred=None
    )
    assert (value, source) == (None, "none")
    w = _weight(completion=None, completion_source="none")
    assert w.weight_current == 0.0 and w.mode_score is None


# --- 모드 정규화 -----------------------------------------------------------


def test_same_completion_weighs_more_when_mode_base_is_low() -> None:
    """80% 완주는 clickrow(기저 0.40)에서 강한 증거, trackdone(0.84)에서는 거의 정보가 없다.

    평평한 가산점을 주는 설계는 이 관계를 정확히 뒤집는다 (2026-08-16 실측).
    """
    deliberate = _weight(completion=0.8, mode="clickrow")
    passive = _weight(completion=0.8, mode="trackdone")
    assert deliberate.weight_core > passive.weight_core
    assert deliberate.mode_score > 0 > passive.mode_score


def test_unknown_mode_falls_back_to_global_base() -> None:
    w = _weight(completion=0.7, mode="live:track_change")
    assert abs(w.mode_score - (0.7 - PARAMS.global_base_rate)) < 1e-9


# --- 이탈 -----------------------------------------------------------------


def test_immediate_abandon_is_negative() -> None:
    assert _weight(completion=0.01, mode="trackdone").weight_core < 0


def test_deliberate_abandon_is_punished_harder() -> None:
    """직접 골라 놓고 끈 것과 흘러오다 끈 것은 뜻이 다르다."""
    chosen = _weight(completion=0.01, mode="clickrow")
    drifted = _weight(completion=0.01, mode="trackdone")
    assert chosen.weight_core < drifted.weight_core


# --- 자발성과 감쇠 ---------------------------------------------------------


def test_self_context_outweighs_algorithmic() -> None:
    assert (
        _weight(context_origin="self").weight_core
        > _weight(context_origin="algorithmic").weight_core
    )


def test_unknown_origin_is_neutral() -> None:
    assert _weight(context_origin=None).voluntary == 1.0
    assert _weight(context_origin="external").voluntary == 1.0


def test_current_decays_faster_than_core() -> None:
    """반감기가 둘인 이유 — 회전층과 기저층이 겹쳐 있다."""
    old = _weight(age_ms=365 * DAY)
    assert old.weight_current < old.weight_core
    fresh = _weight(age_ms=0)
    assert abs(fresh.weight_current - fresh.weight_core) < 1e-9


def test_half_life_is_actually_half() -> None:
    fresh = _weight(age_ms=0)
    aged = _weight(age_ms=int(PARAMS.half_life_days_current * DAY))
    assert abs(aged.weight_current / fresh.weight_current - 0.5) < 1e-6


# --- 드라이버 --------------------------------------------------------------


def _session(db: sqlite3.Connection, **over) -> int:
    row = {
        "track_uri": "spotify:track:T1",
        "started_at": now_ms() - DAY,
        "ms_played": 180_000,
        "source": "history_export",
        "timing_confidence": "low",
        "reason_start": "trackdone",
        "created_at": now_ms(),
        "updated_at": now_ms(),
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join(["?"] * len(row))
    with tx(db):
        cur = db.execute(f"INSERT INTO play_session ({cols}) VALUES ({marks})", tuple(row.values()))
    return int(cur.lastrowid or 0)


def _track(db: sqlite3.Connection, uri="spotify:track:T1", *, duration=200_000) -> None:
    with tx(db):
        db.execute(
            "INSERT INTO track (uri, name, duration_ms, album_uri, fetch_status) "
            "VALUES (?, '곡', ?, 'spotify:album:A1', 'ok') ON CONFLICT (uri) DO NOTHING",
            (uri, duration),
        )
        db.execute(
            "INSERT INTO artist (uri, name, fetch_status) VALUES ('spotify:artist:AR1', 'A', 'ok') "
            "ON CONFLICT (uri) DO NOTHING"
        )
        db.execute(
            "INSERT INTO track_artist (track_uri, artist_uri, position) VALUES (?, ?, 0) "
            "ON CONFLICT (track_uri, position) DO NOTHING",
            (uri, "spotify:artist:AR1"),
        )


def test_build_is_dry_run_by_default(db: sqlite3.Connection) -> None:
    _track(db)
    _session(db)
    create_params_version(db)
    result = build_features(db)
    assert result.sessions == 1
    assert db.execute("SELECT count(*) FROM play_weight").fetchone()[0] == 0


def test_build_writes_weights_and_affinity(db: sqlite3.Connection) -> None:
    _track(db)
    _session(db)
    version = create_params_version(db)
    result = build_features(db, write=True)

    assert result.weighted == 1
    row = db.execute("SELECT * FROM play_weight").fetchone()
    assert row["params_version"] == version
    assert row["completion_source"] == "export"

    artist = db.execute(
        "SELECT * FROM entity_affinity WHERE entity_type='artist' AND horizon='core'"
    ).fetchone()
    assert artist["entity_key"] == "spotify:artist:AR1"
    assert artist["plays"] == 1


def test_params_version_is_pinned_not_overwritten(db: sqlite3.Connection) -> None:
    """반감기를 바꿔 다시 계산해도 옛 점수가 사라지면 안 된다."""
    _track(db)
    _session(db)
    v1 = create_params_version(db)
    build_features(db, write=True)
    v2 = create_params_version(db, note="반감기 실험")
    build_features(db, write=True)

    versions = {r[0] for r in db.execute("SELECT DISTINCT params_version FROM play_weight")}
    assert versions == {v1, v2}


def test_base_rates_are_frozen_into_the_version(db: sqlite3.Connection) -> None:
    """기저율을 매번 다시 재면 과거 점수가 소급해 바뀐다."""
    _track(db)
    for i in range(40):
        _session(db, started_at=now_ms() - DAY - i * 1_000, ms_played=180_000)
    version = create_params_version(db)
    _, params = load_params(db, version)
    assert params.mode_base_rates["trackdone"] > 0

    # 데이터가 더 쌓여도 그 버전의 기저율은 그대로다.
    for i in range(40):
        _session(db, started_at=now_ms() - 2 * DAY - i * 1_000, ms_played=10_000)
    _, again = load_params(db, version)
    assert again.mode_base_rates == params.mode_base_rates


def test_missing_params_version_is_reported(db: sqlite3.Connection) -> None:
    try:
        build_features(db)
    except LookupError as exc:
        assert "features params --new" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("파라미터가 없으면 알려 줘야 한다")
