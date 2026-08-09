"""세션 상태머신 — 이 프로젝트에서 가장 값진 테스트 파일.

여기가 틀리면 뒤의 모든 것(취향 모델, 추천, 평가)이 무의미해진다.
"""

from __future__ import annotations

import pytest

from spotify_taste.sessions.machine import MachineConfig, SessionMachine
from spotify_taste.sessions.model import PlayerSample, SampleKind

from ._builders import Trajectory, run

A = "spotify:track:AAAA"
B = "spotify:track:BBBB"
THREE_MIN = 180_000


# --- 기본 재생 ------------------------------------------------------------


def test_full_play_then_next_track() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(175).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())

    assert len(emitted) == 1
    s = emitted[0]
    assert s.track_uri == A
    assert s.end_reason == "track_change"
    assert s.ms_played == pytest.approx(175_000, abs=2_000)
    assert s.completion_ratio == pytest.approx(0.97, abs=0.02)
    assert s.listened_ratio == pytest.approx(0.97, abs=0.02)


def test_open_session_is_exposed_for_upsert() -> None:
    """폴러는 매 틱 열린 세션을 is_open=1 로 저장한다 (크래시 안전성)."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50)
    emitted, machine = run(traj.samples())
    assert emitted == []
    assert machine.open_session is not None
    assert machine.open_session.track_uri == A


def test_nothing_emitted_while_still_playing() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(100)
    emitted, _ = run(traj.samples())
    assert emitted == []


# --- 스킵 (가장 값진 negative 라벨) ---------------------------------------


def test_eight_second_skip_is_emitted_not_dropped() -> None:
    """8초 스킵은 이 데이터셋에서 가장 값진 negative 라벨이다."""
    traj = (
        Trajectory(poll_ms=4_000)
        .play(A, duration=THREE_MIN)
        .advance(8, ticks=2)
        .next(B, duration=THREE_MIN)
    )
    emitted, _ = run(traj.samples())

    assert len(emitted) == 1
    assert emitted[0].track_uri == A
    assert emitted[0].completion_ratio < 0.15
    assert emitted[0].end_reason == "track_change"


def test_immediate_skip_below_min_emit_still_recorded() -> None:
    """track_change 는 MIN_EMIT_MS 를 우회한다 — 즉시 스킵도 신호다."""
    traj = Trajectory(poll_ms=500).play(A, duration=THREE_MIN).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert len(emitted) == 1
    assert emitted[0].track_uri == A
    assert emitted[0].ms_played == 0


# --- 일시정지 -------------------------------------------------------------


def test_pause_does_not_accumulate_listen_time() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).pause(ticks=4).advance(50)
    emitted, machine = run(traj.samples())
    assert emitted == []
    # 100초 재생 + 100초 정지 → 청취는 100초만.
    assert machine.open_session is not None
    assert machine.open_session.ms_played_accum == pytest.approx(100_000, abs=3_000)


def test_pause_then_resume_stays_one_session() -> None:
    traj = (
        Trajectory()
        .play(A, duration=THREE_MIN)
        .advance(50)
        .pause(ticks=2)
        .advance(50)
        .next(B, duration=THREE_MIN)
    )
    emitted, _ = run(traj.samples())
    assert len(emitted) == 1


def test_long_pause_closes_the_session() -> None:
    """30분 넘게 멈춰 있으면 그건 더 이상 하나의 청취가 아니다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).pause(ticks=80)  # 33분
    emitted, machine = run(traj.samples())
    assert len(emitted) == 1
    assert emitted[0].end_reason == "pause_timeout"
    assert machine.open_session is None


def test_pause_timeout_end_time_is_last_real_progress() -> None:
    """정지해 있던 30분을 세션 길이에 넣으면 안 된다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50)
    last_playing_at = traj.now
    traj.pause(ticks=80)
    emitted, _ = run(traj.samples())
    assert emitted[0].ended_at == last_playing_at


# --- 되감기와 반복 재생 ---------------------------------------------------


def test_rewind_splits_into_two_sessions() -> None:
    traj = (
        Trajectory()
        .play(A, duration=THREE_MIN)
        .advance(100)
        .seek_to(10_000)  # 되감기
        .advance(50)
        .next(B, duration=THREE_MIN)
    )
    emitted, _ = run(traj.samples())
    assert [s.track_uri for s in emitted] == [A, A]
    assert emitted[0].end_reason == "rewind"


def test_replay_from_zero_splits() -> None:
    """한 곡을 5번 반복한 것이 한 번의 긴 재생으로 뭉개지면 안 된다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(175).seek_to(0).advance(175)
    traj.next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert len(emitted) == 2
    assert all(s.track_uri == A for s in emitted)
    # started_at 이 달라야 UNIQUE(track_uri, started_at) 을 통과한다.
    assert emitted[0].started_at != emitted[1].started_at


def test_small_backward_jitter_does_not_split() -> None:
    """기기 시계 오차로 progress 가 살짝 뒤로 가는 일이 있다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).jitter(-2_000).advance(50)
    emitted, _ = run(traj.samples())
    assert emitted == []


def test_forward_jitter_does_not_count_as_seek() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).jitter(+1_500).advance(50)
    _, machine = run(traj.samples())
    assert machine.open_session is not None
    assert machine.open_session.seek_forward_count == 0


# --- 앞으로 탐색 ----------------------------------------------------------


def test_seek_forward_is_not_credited_as_listening() -> None:
    """건너뛴 구간은 듣지 않았다. 적립하면 listened_ratio 가 거짓말을 한다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(20).seek_to(150_000).advance(25)
    traj.next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())

    s = emitted[0]
    assert s.seek_forward_count == 1
    # 위치는 끝까지 갔지만 실제로 들은 건 45초 남짓.
    assert s.completion_ratio > 0.95
    assert s.listened_ratio == pytest.approx(45_000 / THREE_MIN, abs=0.05)


def test_scrub_to_end_is_not_a_positive_label() -> None:
    """이 두 지표를 하나로 뭉개면 안 되는 이유 전체 (C2)."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(5).seek_to(175_000).advance(5)
    traj.next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    s = emitted[0]
    assert s.completion_ratio >= 0.9  # 위치 기준으로는 '완주'
    assert s.listened_ratio < 0.6  # 하지만 실제로 들은 건 거의 없다


# --- 유휴(204) ------------------------------------------------------------


def test_single_idle_does_not_close_session() -> None:
    """204 는 정상 상태다. 잠깐 끊겼다고 세션을 닫으면 안 된다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).idle(ticks=1)
    emitted, machine = run(traj.samples())
    assert emitted == []
    assert machine.open_session is not None


def test_prolonged_idle_closes_session() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).idle(ticks=80)
    emitted, machine = run(traj.samples())
    assert len(emitted) == 1
    assert emitted[0].end_reason == "idle_timeout"
    assert machine.open_session is None


def test_idle_before_any_play_is_harmless() -> None:
    emitted, machine = run(Trajectory().idle(ticks=10).samples())
    assert emitted == []
    assert machine.open_session is None


def test_playing_resumes_after_idle_gap() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).idle(ticks=2).advance(50)
    emitted, machine = run(traj.samples())
    assert emitted == []
    assert machine.open_session is not None
    assert machine.open_session.track_uri == A


# --- 오류 -----------------------------------------------------------------


def test_error_does_not_close_or_credit() -> None:
    """오류 구간은 아무것도 알 수 없다. 청취 시간으로 세면 안 된다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50)
    before = run(traj.samples())[1].open_session
    assert before is not None
    accumulated = before.ms_played_accum

    traj.error(ticks=3)
    emitted, machine = run(traj.samples())
    assert emitted == []
    assert machine.open_session is not None
    assert machine.open_session.ms_played_accum == accumulated


def test_error_is_not_treated_as_idle() -> None:
    """오류를 유휴로 착각하면 활발한 청취 중에 조용히 세션이 닫힌다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).error(ticks=200)
    emitted, machine = run(traj.samples())
    assert emitted == []
    assert machine.open_session is not None


# --- 트랙 메타 변화 -------------------------------------------------------


def test_duration_change_midsession_uses_latest() -> None:
    """track relinking 으로 재생 도중 duration 이 바뀔 수 있다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).relink(duration=200_000)
    traj.advance(50).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert emitted[0].duration_ms == 200_000


def test_missing_duration_yields_null_ratios() -> None:
    """duration 을 모르면 비율을 지어내지 않는다."""
    samples = [
        PlayerSample(sampled_at=0, is_playing=True, track_uri=A, duration_ms=None, progress_ms=0),
        PlayerSample(
            sampled_at=25_000, is_playing=True, track_uri=A, duration_ms=None, progress_ms=25_000
        ),
        PlayerSample(
            sampled_at=50_000, is_playing=True, track_uri=B, duration_ms=None, progress_ms=0
        ),
    ]
    emitted, _ = run(samples)
    assert emitted[0].completion_ratio is None
    assert emitted[0].listened_ratio is None
    assert emitted[0].ms_played == 25_000


def test_non_track_content_treated_as_idle() -> None:
    """광고나 알 수 없는 타입은 track_uri 가 없다."""
    samples = [
        PlayerSample(sampled_at=0, is_playing=True, track_uri=A, duration_ms=1000, progress_ms=0),
        PlayerSample(sampled_at=25_000, is_playing=True, track_uri=None, progress_ms=None),
    ]
    emitted, machine = run(samples)
    assert emitted == []
    assert machine.open_session is not None  # 유휴처럼 열어 둔다


# --- 스크럽 아티팩트 ------------------------------------------------------


def test_zero_length_session_dropped_on_idle_timeout() -> None:
    """재생 없이 끝난 세션은 스크럽 아티팩트지 청취가 아니다."""
    traj = Trajectory().play(A, duration=THREE_MIN).idle(ticks=80)
    emitted, _ = run(traj.samples())
    assert emitted == []


# --- 컨텍스트 -------------------------------------------------------------


def test_context_captured_from_first_sample() -> None:
    traj = Trajectory().context("spotify:playlist:PL", "playlist")
    traj.play(A, duration=THREE_MIN).advance(50).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert emitted[0].context_uri == "spotify:playlist:PL"
    assert emitted[0].context_type == "playlist"


def test_late_arriving_context_is_backfilled() -> None:
    """계정에 따라 context 가 뒤늦게 채워져 오는 경우가 있다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(25)
    traj.context("spotify:playlist:PL", "playlist").advance(25)
    traj.next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert emitted[0].context_uri == "spotify:playlist:PL"


def test_device_and_shuffle_are_captured() -> None:
    """Phase 4 의 컨텍스트 인지 추천은 이 필드들 없이는 불가능하고,
    소급 수집도 안 된다."""
    traj = Trajectory().device("smartphone", shuffle=True)
    traj.play(A, duration=THREE_MIN).advance(50).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert emitted[0].device_type == "smartphone"
    assert emitted[0].shuffle is True


# --- 중도 합류 ------------------------------------------------------------


def test_joining_mid_track_is_flagged() -> None:
    """폴러가 재생 중에 시작하면 ms_played 는 하한이다."""
    traj = Trajectory().play(A, duration=THREE_MIN, progress=120_000).advance(50)
    traj.next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert emitted[0].joined_mid_track is True
    assert emitted[0].completion_ratio > 0.9  # 위치는 끝까지
    assert emitted[0].listened_ratio < 0.4  # 하지만 관측한 건 일부


# --- 강제 종료 ------------------------------------------------------------


def test_close_emits_open_session() -> None:
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50)
    _, machine = run(traj.samples())
    emitted = machine.close(end_reason="process_restart", ended_at=traj.now)
    assert len(emitted) == 1
    assert emitted[0].end_reason == "process_restart"
    assert machine.open_session is None


def test_close_with_no_open_session_is_noop() -> None:
    machine = SessionMachine()
    assert machine.close(end_reason="process_restart", ended_at=0) == []


def test_rehydrate_continues_a_session() -> None:
    """재시작 후 반열림 세션을 이어받아야 한 곡이 두 세션으로 갈라지지 않는다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50)
    _, machine1 = run(traj.samples())
    open_ = machine1.open_session
    assert open_ is not None

    machine2 = SessionMachine()
    machine2.rehydrate(open_)
    traj.advance(50).next(B, duration=THREE_MIN)
    emitted = []
    for s in traj.samples()[len(traj.samples()) - 3 :]:
        emitted.extend(machine2.on_sample(s))
    assert any(e.track_uri == A for e in emitted)


# --- 설정 -----------------------------------------------------------------


def test_config_is_tunable_for_recompute() -> None:
    """상수를 바꾸면 `sessions rebuild` 로 과거를 다시 계산할 수 있어야 한다."""
    traj = Trajectory().play(A, duration=THREE_MIN).advance(50).seek_to(48_000)
    traj.advance(20).next(B, duration=THREE_MIN)

    strict, _ = run(traj.samples(), rewind_threshold_ms=1_000)
    lenient, _ = run(traj.samples(), rewind_threshold_ms=10_000)
    assert len(strict) > len(lenient)


def test_machine_config_defaults_match_plan() -> None:
    c = MachineConfig()
    assert (c.rewind_threshold_ms, c.seek_tolerance_ms) == (3_000, 2_000)
    assert (c.pause_timeout_ms, c.min_emit_ms) == (1_800_000, 1_000)


# --- 순수성 ---------------------------------------------------------------


def test_machine_never_reads_the_wall_clock(monkeypatch) -> None:
    """시간은 오직 샘플의 sampled_at 에서만 온다. 이게 깨지면 rebuild 가
    실시간 결과를 재현할 수 없다."""
    import time

    def boom(*a, **k):
        raise AssertionError("상태머신이 벽시계를 읽었다")

    monkeypatch.setattr(time, "time", boom)
    monkeypatch.setattr(time, "monotonic", boom)

    traj = Trajectory().play(A, duration=THREE_MIN).advance(175).next(B, duration=THREE_MIN)
    emitted, _ = run(traj.samples())
    assert len(emitted) == 1


def test_same_samples_give_identical_result_twice() -> None:
    """결정적이어야 rebuild 가 의미를 갖는다."""
    traj = (
        Trajectory()
        .play(A, duration=THREE_MIN)
        .advance(100)
        .pause(ticks=2)
        .advance(50)
        .seek_to(0)
        .advance(60)
        .idle(ticks=2)
        .next(B, duration=THREE_MIN)
        .advance(10)
    )
    first, _ = run(traj.samples())
    second, _ = run(traj.samples())
    assert first == second


def test_sample_kind_values_are_stable() -> None:
    """DB 와 로그에 문자열로 남으므로 값이 바뀌면 과거 데이터가 안 맞는다."""
    assert SampleKind.OK == "ok"
    assert SampleKind.IDLE == "idle"
    assert SampleKind.ERROR == "error"
