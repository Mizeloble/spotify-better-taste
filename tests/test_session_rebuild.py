"""재구성 == 실시간.

이 파일 하나가 "수집과 해석의 분리"라는 설계 원칙의 보증서다. 두 출력이
갈라지는 순간, 규칙을 바꿔 과거를 다시 계산한다는 계획 전체가 무너진다.
"""

from __future__ import annotations

import sqlite3

import pytest

from spotify_taste.db.connection import tx
from spotify_taste.sessions.machine import MachineConfig, SessionMachine
from spotify_taste.sessions.rebuild import load_samples, rebuild_sessions
from spotify_taste.sessions.store import insert_player_sample

from ._builders import Trajectory

A = "spotify:track:AAAA"
B = "spotify:track:BBBB"
C = "spotify:track:CCCC"
THREE_MIN = 180_000


def _store(db: sqlite3.Connection, samples) -> None:
    with tx(db):
        for s in samples:
            insert_player_sample(db, s)


def _live(samples, config: MachineConfig | None = None):
    """실시간 폴러가 만들었을 결과."""
    machine = SessionMachine(config)
    out = []
    for s in samples:
        out.extend(machine.on_sample(s))
    return out


def _comparable(sessions):
    """샘플 id 를 뺀 비교용 형태.

    id 는 DB 채번이라 실시간 경로에서는 None 이다. 판정 결과가 같은지만 본다.
    """
    return [
        (
            s.track_uri,
            s.started_at,
            s.ended_at,
            s.ms_played,
            s.max_progress_ms,
            s.completion_ratio,
            s.listened_ratio,
            s.end_reason,
            s.context_uri,
            s.device_type,
            s.shuffle,
            s.sample_count,
            s.seek_forward_count,
        )
        for s in sessions
    ]


# --- 핵심 보증 ------------------------------------------------------------


def test_rebuild_matches_live_for_a_complex_trajectory(db: sqlite3.Connection) -> None:
    """되감기·일시정지·유휴·스킵·탐색이 뒤섞인 궤적에서도 같아야 한다."""
    traj = (
        Trajectory()
        .context("spotify:playlist:PL", "playlist")
        .play(A, duration=THREE_MIN)
        .advance(100)
        .pause(ticks=2)
        .advance(50)
        .seek_to(0)  # 반복 재생
        .advance(60)
        .idle(ticks=2)
        .next(B, duration=THREE_MIN)
        .advance(8)  # 스킵
        .next(C, duration=THREE_MIN)
        .advance(30)
        .seek_to(170_000)  # 앞으로 탐색
        .advance(10)
    )
    samples = traj.samples()
    _store(db, samples)

    live = _live(samples)
    rebuilt = rebuild_sessions(db).sessions
    assert _comparable(rebuilt) == _comparable(live)
    assert len(live) >= 3


def test_rebuild_matches_live_with_errors(db: sqlite3.Connection) -> None:
    """오류 샘플도 원시 기록으로 남으므로 재구성이 같아야 한다."""
    traj = (
        Trajectory()
        .play(A, duration=THREE_MIN)
        .advance(50)
        .error(ticks=3)
        .advance(50)
        .next(B, duration=THREE_MIN)
    )
    samples = traj.samples()
    _store(db, samples)
    assert _comparable(rebuild_sessions(db).sessions) == _comparable(_live(samples))


def test_rebuild_matches_live_for_pure_idle(db: sqlite3.Connection) -> None:
    traj = Trajectory().idle(ticks=20)
    samples = traj.samples()
    _store(db, samples)
    assert rebuild_sessions(db).sessions == _live(samples) == []


# --- 샘플 종류 복원 -------------------------------------------------------


def test_sample_kinds_survive_the_roundtrip(db: sqlite3.Connection) -> None:
    """종류를 컬럼으로 저장하지 않으므로 status/track_uri 에서 되살린다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(25).idle(ticks=1).error(ticks=1)
    _store(db, traj.samples())
    kinds = [s.kind for s in load_samples(db)]
    assert kinds == ["ok", "ok", "idle", "error"]


def test_loaded_samples_carry_row_ids(db: sqlite3.Connection) -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(25)
    _store(db, traj.samples())
    assert all(s.id is not None for s in load_samples(db))


# --- 구간 지정 ------------------------------------------------------------


def test_rebuild_respects_since_and_until(db: sqlite3.Connection) -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100)
    cutoff = traj.now
    traj.next(B, duration=THREE_MIN).advance(100)
    _store(db, traj.samples())

    early = rebuild_sessions(db, until=cutoff)
    assert early.sample_count < rebuild_sessions(db).sample_count


def test_open_session_at_range_end_is_not_forced_closed(db: sqlite3.Connection) -> None:
    """구간 끝에서 강제로 닫으면 실시간 결과와 달라지고, 진행 중인 재생이
    잘린다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100)
    _store(db, traj.samples())
    assert rebuild_sessions(db).sessions == []


# --- 쓰기 -----------------------------------------------------------------


def test_dry_run_writes_nothing(db: sqlite3.Connection) -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100).next(B, duration=THREE_MIN)
    _store(db, traj.samples())

    result = rebuild_sessions(db, write=False)
    assert len(result.sessions) == 1
    assert result.written == 0
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 0


def test_write_persists_sessions(db: sqlite3.Connection) -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100).next(B, duration=THREE_MIN)
    _store(db, traj.samples())

    with tx(db):
        result = rebuild_sessions(db, write=True)
    assert result.written == 1
    row = db.execute("SELECT track_uri, is_open FROM play_session").fetchone()
    assert (row["track_uri"], row["is_open"]) == (A, 0)


def test_rebuild_is_idempotent(db: sqlite3.Connection) -> None:
    """같은 샘플을 두 번 재생해도 행이 늘어나면 안 된다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100).next(B, duration=THREE_MIN)
    _store(db, traj.samples())
    with tx(db):
        rebuild_sessions(db, write=True)
    with tx(db):
        rebuild_sessions(db, write=True)
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


# --- 규칙 변경 ------------------------------------------------------------


def test_changed_rules_produce_different_sessions(db: sqlite3.Connection) -> None:
    """이게 되기 때문에 원시 샘플을 영구 보관하는 것이다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).seek_to(48_000)
    traj.advance(20).next(B, duration=THREE_MIN)
    _store(db, traj.samples())

    strict = rebuild_sessions(db, config=MachineConfig(rewind_threshold_ms=1_000)).sessions
    lenient = rebuild_sessions(db, config=MachineConfig(rewind_threshold_ms=10_000)).sessions
    assert len(strict) > len(lenient)


@pytest.mark.parametrize("pause_timeout_ms", [60_000, 1_800_000])
def test_pause_timeout_is_recomputable(db: sqlite3.Connection, pause_timeout_ms: int) -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).pause(ticks=10)  # 4분 정지
    _store(db, traj.samples())
    sessions = rebuild_sessions(
        db, config=MachineConfig(pause_timeout_ms=pause_timeout_ms)
    ).sessions
    # 1분 타임아웃이면 닫히고, 30분이면 아직 열려 있다.
    assert len(sessions) == (1 if pause_timeout_ms == 60_000 else 0)
