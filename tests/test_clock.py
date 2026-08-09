"""주입 가능한 시계."""

from __future__ import annotations

from spotify_taste.clock import Clock, FrozenClock, SystemClock


def test_system_clock_satisfies_protocol() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FrozenClock(), Clock)


def test_frozen_clock_does_not_move_on_its_own() -> None:
    c = FrozenClock(start_ms=1_000)
    assert c.now_ms() == c.now_ms() == 1_000


def test_advance_moves_wall_and_monotonic_together() -> None:
    c = FrozenClock(start_ms=1_000, start_monotonic=0.0)
    c.advance(seconds=30)
    assert c.now_ms() == 31_000
    assert c.monotonic() == 30.0


def test_advance_accepts_milliseconds() -> None:
    c = FrozenClock()
    c.advance(ms=250)
    assert c.now_ms() == 250
    assert c.monotonic() == 0.25


def test_set_ms_jumps_wall_clock_only() -> None:
    """NTP 보정으로 벽시계가 뛰어도 monotonic 은 그대로여야 한다 —
    레이트리미터의 데드라인이 monotonic 을 쓰는 이유."""
    c = FrozenClock(start_ms=1_000, start_monotonic=5.0)
    c.set_ms(999_000)
    assert c.now_ms() == 999_000
    assert c.monotonic() == 5.0


def test_system_clock_monotonic_is_non_decreasing() -> None:
    c = SystemClock()
    assert c.monotonic() <= c.monotonic()
