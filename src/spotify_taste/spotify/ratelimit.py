"""무엇/왜
Spotify 호출 속도를 제어한다. 복수 조회 엔드포인트가 전부 사라져 카탈로그
해석이 1엔티티=1콜이 된 지금, 레이트리밋은 "가끔 걸리는 것"이 아니라 상시
관리 대상이다.

설계 원칙
- **리미터는 프로세스에 하나다.** Spotify 는 커넥션이 아니라 client_id 단위로
  제한하므로, 역할별로 리미터를 나누면 자기기만이 된다. 역할은 *하한 간격*만
  다르게 갖는다.
- **데드라인은 락 안에서 계산하고 sleep 은 락 밖에서 한다.** 긴 대기 하나가
  뮤텍스를 잡은 채 전체를 직렬화하면, 429 한 번이 수집 전체를 멈춘다.
- **폴러는 백필 뒤에 줄서지 않는다.** `acquire(timeout=…)` 으로 슬롯을 못 얻으면
  틱을 건너뛴다. 샘플 하나를 버리는 게 낡은 샘플 백로그를 쌓는 것보다 항상 낫다.
- **적응형**: 429 마다 간격을 1.5배로 넓히고, 연속 성공이 쌓이면 하한 쪽으로
  되돌린다. 공개되지 않은 실제 한도를 추측 대신 찾아간다.
- **`Retry-After` 가 너무 길면 자지 않는다.** 수집기 안에서 한 시간 자는 것은
  행(hang)과 구별되지 않는다 — 배치를 접고 다음 예약 실행에 넘긴다.

어떻게 확인
    uv run pytest tests/test_ratelimit.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

from ..clock import Clock, SystemClock

__all__ = ["ROLE_FLOOR_SEC", "RateLimiter", "RetryAfterTooLong"]

log = structlog.get_logger(__name__)

# 역할별 최소 간격(초). 스스로 정한 지속 2 req/s 상한 안에서 나눈 값이다.
ROLE_FLOOR_SEC: dict[str, float] = {
    # 25초 루프가 자연 페이싱을 하므로 하한은 코드 버그로 인한 버스트 방어용.
    "poller": 0.2,
    "recent": 0.2,
    "snapshot": 0.3,
    # 2 rps 상한 → 30초 창에 최대 60건.
    "backfill": 0.5,
    "cli": 0.3,
}
DEFAULT_FLOOR_SEC = 0.3

# 429 를 만날 때마다 간격에 곱하는 값과 그 상한.
BACKOFF_FACTOR = 1.5
MAX_MULTIPLIER = 8.0
# 이만큼 연속 성공하면 배수를 한 단계 되돌린다.
DECAY_AFTER_SUCCESSES = 50
DECAY_FACTOR = 0.95


class RetryAfterTooLong(Exception):
    """`Retry-After` 가 감당할 수 없이 길다. 배치를 접어야 한다."""

    def __init__(self, retry_after_sec: float) -> None:
        super().__init__(f"Retry-After 가 {retry_after_sec:.0f}초입니다 — 배치를 중단합니다")
        self.retry_after_sec = retry_after_sec


class RateLimiter:
    """공유 호출 속도 제어기."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        max_retry_after_sec: float = 300.0,
        floors: dict[str, float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._max_retry_after = max_retry_after_sec
        self._floors = floors if floors is not None else dict(ROLE_FLOOR_SEC)
        # sleep 을 주입 가능하게 두면 테스트가 실제로 기다리지 않아도 된다.
        self._sleep = sleep or asyncio.sleep

        self._lock = asyncio.Lock()
        self._next_allowed = 0.0  # monotonic 기준
        self._not_before = 0.0  # 429 Retry-After 데드라인
        self._multiplier = 1.0
        self._consecutive_successes = 0

    # --- 상태 조회 (진단용) ----------------------------------------------

    @property
    def multiplier(self) -> float:
        return self._multiplier

    def interval_for(self, role: str) -> float:
        """현재 이 역할이 지켜야 하는 간격(초)."""
        return self._floors.get(role, DEFAULT_FLOOR_SEC) * self._multiplier

    # --- 슬롯 획득 --------------------------------------------------------

    async def acquire(self, role: str, *, timeout: float | None = None) -> bool:
        """호출 슬롯 하나를 얻는다.

        `timeout` 안에 얻을 수 없으면 **슬롯을 예약하지 않고** False 를
        돌려준다. 폴러는 이때 틱을 건너뛰고 공백을 기록한다.
        """
        async with self._lock:
            now = self._clock.monotonic()
            earliest = max(now, self._next_allowed, self._not_before)
            wait = earliest - now
            if timeout is not None and wait > timeout:
                # 슬롯을 잡지 않고 물러난다 — 잡아 두면 뒤 호출자가 헛기다린다.
                return False
            self._next_allowed = earliest + self.interval_for(role)

        # sleep 은 반드시 락 밖에서. 안에서 자면 한 번의 긴 대기가 전체를
        # 직렬화한다.
        if wait > 0:
            await self._sleep(wait)
        return True

    # --- 응답 피드백 ------------------------------------------------------

    def note_success(self) -> None:
        """성공한 호출. 충분히 쌓이면 간격을 하한 쪽으로 되돌린다."""
        self._consecutive_successes += 1
        if self._consecutive_successes >= DECAY_AFTER_SUCCESSES and self._multiplier > 1.0:
            self._consecutive_successes = 0
            self._multiplier = max(1.0, self._multiplier * DECAY_FACTOR)
            log.debug("spotify.rate_relaxed", multiplier=round(self._multiplier, 3))

    def note_rate_limited(self, retry_after_sec: float | None) -> float:
        """429 를 받았다. 대기해야 할 시간(초)을 돌려준다.

        `Retry-After` 가 상한을 넘으면 `RetryAfterTooLong` 을 올린다 — 자는 게
        아니라 배치를 접는 것이 맞다.
        """
        self._consecutive_successes = 0
        self._multiplier = min(MAX_MULTIPLIER, self._multiplier * BACKOFF_FACTOR)

        wait = float(retry_after_sec if retry_after_sec is not None else 1.0)
        if wait > self._max_retry_after:
            log.warning("spotify.rate_limit_long", retry_after_sec=wait)
            raise RetryAfterTooLong(wait)

        # 데드라인만 설정하고 여기서 자지 않는다. 실제 대기는 다음 acquire() 가
        # 락 밖에서 처리한다.
        self._not_before = max(self._not_before, self._clock.monotonic() + wait)
        log.warning(
            "spotify.rate_limited",
            retry_after_sec=wait,
            multiplier=round(self._multiplier, 3),
        )
        return wait
