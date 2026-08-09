"""무엇/왜
Telegram 알림. 수집기가 조용히 죽는 것을 사람이 알아채게 하는 유일한 채널이다.

설계 원칙
- **자기 예외를 전부 삼킨다.** 알림이 트레이딩을 멈추면 안 된다는 형제
  프로젝트의 규칙이 여기서도 그대로다 — 알림이 수집을 멈추면 안 된다.
- **동기 httpx.** 알림은 드물고(쿨다운 30분) 짧으며, `heartbeat-check` 같은
  동기 CLI 에서도 불린다. 비동기로 만들면 호출부마다 이벤트 루프를 챙겨야 한다.
- **토큰은 어디에도 남기지 않는다.** URL 에 토큰이 들어가므로 예외 메시지와
  로그에 URL 을 넣지 않는다. 형제 프로젝트에서 httpx 의 INFO 로그가 봇 토큰을
  1,500줄 유출한 사고가 있었고, `_setup_logging()` 이 그걸 막고 있지만 여기서도
  이중으로 방어한다.

어떻게 확인
    uv run spotify-taste ops test-alert
    uv run pytest tests/test_notify_telegram.py
"""

from __future__ import annotations

import httpx
import structlog

from .base import AlertKind
from .cooldown import CooldownGate

__all__ = ["TelegramNotifier"]

log = structlog.get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SEC = 10.0
# Telegram 메시지 상한은 4096자. 여유를 두고 자른다.
MAX_BODY_CHARS = 3_500


class TelegramNotifier:
    """봇 API 로 메시지를 보낸다. 종류별 쿨다운이 걸린다."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        cooldown: CooldownGate,
        *,
        client: httpx.Client | None = None,
        prefix: str = "🎧 spotify-taste",
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._cooldown = cooldown
        self._prefix = prefix
        # 주입 가능해야 respx 로 테스트할 수 있다.
        self._client = client
        self._owns_client = client is None

    def send(self, kind: str, title: str, body: str = "") -> bool:
        """알림 전송. 예외를 올리지 않는다."""
        cooldown_sec = AlertKind.COOLDOWNS.get(kind, AlertKind.DEFAULT_COOLDOWN_SEC)
        if not self._cooldown.should_send(kind, cooldown_sec):
            log.debug("notify.cooldown_suppressed", kind=kind, cooldown_sec=cooldown_sec)
            return False

        text = self._format(title, body)
        try:
            client = self._client or httpx.Client(
                base_url=TELEGRAM_API_BASE, timeout=SEND_TIMEOUT_SEC
            )
            try:
                resp = client.post(
                    f"/bot{self._token}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                # raise_for_status() 의 예외 메시지에는 토큰이 담긴 URL 이 들어간다.
                # 상태 코드만 보고 직접 처리한다.
                if resp.status_code >= 400:
                    log.warning("notify.telegram_rejected", kind=kind, status=resp.status_code)
                    return False
            finally:
                if self._owns_client:
                    client.close()
        # 광범위한 except 는 의도적이다 — 알림 실패가 수집을 멈출 수 없다.
        except Exception as exc:
            # str(exc) 에 URL 이 섞일 수 있으므로 타입만 남긴다.
            log.warning("notify.telegram_failed", kind=kind, error=type(exc).__name__)
            return False

        self._cooldown.stamp(kind)
        log.info("notify.sent", kind=kind, title=title)
        return True

    def _format(self, title: str, body: str) -> str:
        head = f"{self._prefix} · {title}"
        if not body:
            return head
        trimmed = body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS] + "\n…(생략)"
        return f"{head}\n\n{trimmed}"

    def __repr__(self) -> str:
        # 설정 객체를 통째로 찍어도 토큰이 새지 않게.
        return f"TelegramNotifier(chat_id={self._chat_id!r})"
