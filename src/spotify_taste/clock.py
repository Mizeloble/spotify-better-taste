"""무엇/왜
시간을 **주입 가능한 의존성**으로 만든다. 세션 상태머신은 30분 일시정지 타임아웃,
120초 재시작 윈도우 같은 긴 시간 상수를 다루는데, 이걸 실제로 기다리는 테스트는
쓸 수 없다.

설계 원칙
- `freezegun` 같은 전역 몽키패치 대신 명시적 주입을 쓴다. 상태머신을 순수하게
  유지하려는 목적과 같은 방향이고, 어떤 코드가 시간을 읽는지 시그니처에 드러난다.
- `now_ms()`(벽시계)와 `monotonic()`(경과 측정)을 분리한다. 레이트리미터의
  `not_before` 데드라인은 NTP 보정에 흔들리면 안 되므로 monotonic 을 쓴다.

어떻게 확인
    uv run pytest tests/test_clock.py
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from ._common import now_ms

__all__ = ["Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """시간 공급자."""

    def now_ms(self) -> int:
        """UTC epoch 밀리초."""
        ...

    def monotonic(self) -> float:
        """단조 증가 초. 경과 시간 측정 전용 (절대 시각 아님)."""
        ...


class SystemClock:
    """운영용. 실제 시계를 읽는다."""

    def now_ms(self) -> int:
        return now_ms()

    def monotonic(self) -> float:
        return time.monotonic()


class FrozenClock:
    """테스트용. 시간이 저절로 흐르지 않고 `advance()` 로만 움직인다."""

    def __init__(self, start_ms: int = 0, start_monotonic: float = 0.0) -> None:
        self._now_ms = start_ms
        self._monotonic = start_monotonic

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, *, ms: int = 0, seconds: float = 0.0) -> None:
        """두 시계를 함께 전진시킨다 (기본은 같은 양만큼 흐른다는 가정)."""
        delta_ms = ms + int(seconds * 1000)
        self._now_ms += delta_ms
        self._monotonic += delta_ms / 1000

    def set_ms(self, value: int) -> None:
        """벽시계만 점프시킨다 (NTP 보정·시계 역행 시나리오 재현용)."""
        self._now_ms = value
