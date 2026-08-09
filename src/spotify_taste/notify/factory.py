"""무엇/왜
설정을 보고 알림 구현체를 고른다. 호출부가 "텔레그램이 설정됐나"를 매번 묻지
않게 하려는 것 — 판단은 여기 한 곳에만 있다.

어떻게 확인
    uv run pytest tests/test_notify_telegram.py
"""

from __future__ import annotations

import structlog

from ..config import Settings, get_settings
from .base import NoopNotifier, Notifier
from .cooldown import CooldownGate
from .telegram import TelegramNotifier

__all__ = ["build_notifier"]

log = structlog.get_logger(__name__)


def build_notifier(settings: Settings | None = None) -> Notifier:
    """설정된 채널이 있으면 그것을, 없으면 NoopNotifier 를 돌려준다."""
    s = settings or get_settings()
    if not s.has_telegram:
        log.debug("notify.channel_unconfigured")
        return NoopNotifier()
    return TelegramNotifier(
        bot_token=s.telegram_bot_token.get_secret_value(),
        chat_id=s.telegram_chat_id,
        cooldown=CooldownGate(s.alert_dir),
    )
