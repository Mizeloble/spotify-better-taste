"""레이트리미터 — 페이싱, 적응, 그리고 '폴러는 줄서지 않는다'.

실제로 자지 않는다: FrozenClock 과 가짜 sleep 을 주입해 시간을 통제한다.
"""

from __future__ import annotations

import asyncio

import pytest

from spotify_taste.clock import FrozenClock
from spotify_taste.spotify.ratelimit import (
    BACKOFF_FACTOR,
    DECAY_AFTER_SUCCESSES,
    MAX_MULTIPLIER,
    RateLimiter,
    RetryAfterTooLong,
)


class FakeSleep:
    """잔 시간을 기록하고 시계를 그만큼 전진시킨다."""

    def __init__(self, clock: FrozenClock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds=seconds)

    @property
    def total(self) -> float:
        return sum(self.slept)


def _limiter(**over):
    clock = FrozenClock(start_monotonic=1_000.0)
    sleeper = FakeSleep(clock)
    limiter = RateLimiter(clock=clock, sleep=sleeper, **over)
    return limiter, clock, sleeper


# --- 페이싱 ---------------------------------------------------------------


async def test_first_acquire_does_not_wait() -> None:
    limiter, _, sleeper = _limiter()
    assert await limiter.acquire("poller") is True
    assert sleeper.total == 0


async def test_second_acquire_waits_the_role_interval() -> None:
    limiter, _, sleeper = _limiter()
    await limiter.acquire("backfill")
    await limiter.acquire("backfill")
    assert sleeper.slept == pytest.approx([0.5])


async def test_roles_share_one_schedule() -> None:
    """Spotify 는 client_id 단위로 제한한다 — 역할별 리미터는 자기기만이다."""
    limiter, _, sleeper = _limiter()
    await limiter.acquire("backfill")  # 다음 슬롯을 0.5초 뒤로 민다
    await limiter.acquire("poller")
    assert sleeper.slept == pytest.approx([0.5])


async def test_interval_scales_with_role_floor() -> None:
    limiter, _, _ = _limiter()
    assert limiter.interval_for("poller") == pytest.approx(0.2)
    assert limiter.interval_for("backfill") == pytest.approx(0.5)
    assert limiter.interval_for("알수없는역할") == pytest.approx(0.3)


# --- 폴러는 줄서지 않는다 -------------------------------------------------


async def test_acquire_times_out_instead_of_queueing() -> None:
    """샘플 하나를 버리는 게 낡은 샘플 백로그를 쌓는 것보다 낫다."""
    limiter, _, _ = _limiter()
    limiter.note_rate_limited(30.0)  # 30초 데드라인
    assert await limiter.acquire("poller", timeout=5.0) is False


async def test_timed_out_acquire_does_not_reserve_a_slot(monkeypatch) -> None:
    """물러난 호출이 슬롯을 잡아 두면 뒤 호출자가 헛기다린다."""
    limiter, clock, sleeper = _limiter()
    limiter.note_rate_limited(30.0)
    assert await limiter.acquire("poller", timeout=5.0) is False

    # 데드라인이 지나면 곧바로 통과해야 한다.
    clock.advance(seconds=31)
    assert await limiter.acquire("poller", timeout=5.0) is True
    assert sleeper.total == 0


async def test_acquire_without_timeout_waits_indefinitely() -> None:
    """백필은 기다려도 된다 — 야간에 조용히 흐르는 게 목적이다."""
    limiter, _, sleeper = _limiter()
    limiter.note_rate_limited(10.0)
    assert await limiter.acquire("backfill") is True
    assert sleeper.total == pytest.approx(10.0)


async def test_sleep_happens_outside_the_lock() -> None:
    """긴 대기 하나가 뮤텍스를 잡으면 429 한 번이 수집 전체를 멈춘다.
    락 밖에서 자야 다른 태스크가 그 사이 슬롯을 계산할 수 있다."""
    limiter, clock, _ = _limiter()
    entered = asyncio.Event()

    async def slow_sleep(seconds: float) -> None:
        entered.set()
        await asyncio.sleep(0)  # 다른 태스크에 제어를 넘긴다
        clock.advance(seconds=seconds)

    limiter._sleep = slow_sleep  # type: ignore[assignment]
    limiter.note_rate_limited(10.0)

    waiter = asyncio.create_task(limiter.acquire("backfill"))
    await entered.wait()
    # 첫 호출자가 자는 동안 두 번째 호출자가 락을 잡을 수 있어야 한다.
    quick = await asyncio.wait_for(limiter.acquire("poller", timeout=0.0), timeout=1.0)
    assert quick is False  # 대기가 필요하다고 판정 — 즉 락은 열려 있었다
    await waiter


# --- 적응형 배수 ----------------------------------------------------------


async def test_429_widens_the_interval() -> None:
    limiter, _, _ = _limiter()
    before = limiter.interval_for("backfill")
    limiter.note_rate_limited(1.0)
    assert limiter.interval_for("backfill") == pytest.approx(before * BACKOFF_FACTOR)


async def test_multiplier_is_capped() -> None:
    limiter, _, _ = _limiter()
    for _ in range(50):
        limiter.note_rate_limited(1.0)
    assert limiter.multiplier == pytest.approx(MAX_MULTIPLIER)


async def test_successes_decay_the_multiplier_back() -> None:
    """추측 대신 실제 한도를 찾아간다 — 넓혔으면 되돌릴 줄도 알아야 한다."""
    limiter, _, _ = _limiter()
    limiter.note_rate_limited(1.0)
    widened = limiter.multiplier
    for _ in range(DECAY_AFTER_SUCCESSES):
        limiter.note_success()
    assert limiter.multiplier < widened


async def test_multiplier_never_drops_below_floor() -> None:
    limiter, _, _ = _limiter()
    for _ in range(DECAY_AFTER_SUCCESSES * 10):
        limiter.note_success()
    assert limiter.multiplier == pytest.approx(1.0)


async def test_429_resets_success_streak() -> None:
    limiter, _, _ = _limiter()
    limiter.note_rate_limited(1.0)
    for _ in range(DECAY_AFTER_SUCCESSES - 1):
        limiter.note_success()
    limiter.note_rate_limited(1.0)  # 스트릭 리셋
    for _ in range(DECAY_AFTER_SUCCESSES - 1):
        limiter.note_success()
    # 아직 감쇠가 일어나면 안 된다 (배수는 두 번의 429 로 넓혀진 상태)
    assert limiter.multiplier == pytest.approx(BACKOFF_FACTOR**2)


# --- Retry-After 상한 -----------------------------------------------------


async def test_long_retry_after_aborts_instead_of_sleeping() -> None:
    """수집기 안에서 한 시간 자는 것은 행(hang)과 구별되지 않는다."""
    limiter, _, sleeper = _limiter(max_retry_after_sec=300.0)
    with pytest.raises(RetryAfterTooLong) as exc:
        limiter.note_rate_limited(3_600.0)
    assert exc.value.retry_after_sec == 3_600.0
    assert sleeper.total == 0


async def test_retry_after_at_the_limit_is_accepted() -> None:
    limiter, _, _ = _limiter(max_retry_after_sec=300.0)
    assert limiter.note_rate_limited(300.0) == pytest.approx(300.0)


async def test_missing_retry_after_uses_a_short_default() -> None:
    limiter, _, _ = _limiter()
    assert limiter.note_rate_limited(None) == pytest.approx(1.0)


async def test_longest_deadline_wins() -> None:
    """짧은 429 가 뒤따라와 긴 데드라인을 앞당기면 안 된다."""
    limiter, _, sleeper = _limiter()
    limiter.note_rate_limited(60.0)
    limiter.note_rate_limited(1.0)
    await limiter.acquire("backfill")
    assert sleeper.total == pytest.approx(60.0)


# --- 예산 ------------------------------------------------------------------


async def test_sustained_rate_stays_under_two_per_second() -> None:
    """스스로 정한 지속 2 rps 상한을 실제로 지키는지."""
    limiter, clock, _ = _limiter()
    start = clock.monotonic()
    for _ in range(60):
        await limiter.acquire("backfill")
    elapsed = clock.monotonic() - start
    assert elapsed >= 29.0  # 60건이 30초 창을 채운다
