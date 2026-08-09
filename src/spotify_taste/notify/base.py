"""무엇/왜
알림 인터페이스와 알림 종류. 수집기는 구체 구현이 아니라 이 Protocol 에만
의존한다 — 테스트에서 갈아 끼우고, 미설정 시 조용히 Noop 으로 떨어뜨리기 위해.

설계 원칙
- **알림 실패가 수집을 멈추면 안 된다.** 구현체는 자기 예외를 삼키고 로그만
  남긴다. 알림은 관측 수단이지 수집의 전제 조건이 아니다.
- 알림 "종류"(`AlertKind`)를 상수로 고정한다. 쿨다운이 종류별이라 오타로 만든
  새 종류는 쿨다운이 영원히 안 걸리는 조용한 버그가 된다.

어떻게 확인
    uv run pytest tests/test_notify_telegram.py
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

import structlog

__all__ = ["AlertKind", "NoopNotifier", "Notifier"]

log = structlog.get_logger(__name__)


class AlertKind:
    """알림 종류. 쿨다운 파일 이름이자 이벤트 이름이다.

    쿨다운은 **종류별**이다. 파일이 하나뿐이면 heartbeat 알림 한 번이 스키마
    변경 알림을 30분간 삼킨다 — 서로 다른 사고를 같은 침묵으로 덮으면 안 된다.
    """

    COLLECTOR_STALE: Final = "collector_stale"
    AUTH_REFRESH_FAILING: Final = "auth_refresh_failing"
    AUTH_REVOKED: Final = "auth_revoked"
    SCHEMA_CHANGED: Final = "schema_changed"
    RATE_LIMIT_LONG: Final = "rate_limit_long"
    DB_INTEGRITY: Final = "db_integrity"
    DAILY_REPORT: Final = "daily_report"
    TEST: Final = "test"

    #: 종류별 기본 쿨다운(초). 여기 없는 종류는 DEFAULT_COOLDOWN_SEC.
    COOLDOWNS: Final[dict[str, int]] = {
        COLLECTOR_STALE: 1_800,  # 30분 — 죽은 수집기를 계속 떠들 필요는 없다
        AUTH_REFRESH_FAILING: 1_800,
        AUTH_REVOKED: 21_600,  # 6시간 — 사람이 재인증해야 풀리므로 길게
        SCHEMA_CHANGED: 3_600,
        RATE_LIMIT_LONG: 3_600,
        DB_INTEGRITY: 86_400,
        DAILY_REPORT: 0,  # 하루 1회 예약 실행이라 쿨다운 불필요
        TEST: 0,
    }

    DEFAULT_COOLDOWN_SEC: Final = 1_800


@runtime_checkable
class Notifier(Protocol):
    """알림 채널."""

    def send(self, kind: str, title: str, body: str = "") -> bool:
        """알림을 보낸다. 실제로 보냈으면 True, 쿨다운/미설정/실패면 False.

        **절대 예외를 올리지 않는다.** 호출부는 반환값을 무시해도 된다.
        """
        ...


class NoopNotifier:
    """알림 채널이 설정되지 않았을 때. 로그만 남기고 아무것도 보내지 않는다."""

    def send(self, kind: str, title: str, body: str = "") -> bool:
        log.info("notify.noop", kind=kind, title=title)
        return False
