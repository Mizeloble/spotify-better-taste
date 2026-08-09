"""무엇/왜
알림 종류별 쿨다운. "같은 사고로 5분마다 알림이 오는" 상태와 "다른 사고가
묻히는" 상태를 동시에 피한다.

설계 원칙
- 종류마다 파일 하나(`<alert_dir>/<kind>.cooldown`). 파일의 **mtime 이 신호**이고
  내용은 사람이 읽을 때만 쓴다 — heartbeat 와 같은 관례.
- 상태를 프로세스 메모리에 두지 않는다. `collect`(상시)와 `heartbeat-check`
  (5분마다 새 프로세스)가 같은 쿨다운을 공유해야 하기 때문이다.
- 파일 I/O 실패는 삼키고 "보내도 된다"로 판정한다. 쿨다운 파일을 못 쓴다고
  알림이 막히면, 정작 중요한 사고를 놓친다.

어떻게 확인
    uv run pytest tests/test_notify_telegram.py
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import structlog

from .._common import epoch_ms_to_iso, now_ms

__all__ = ["CooldownGate"]

log = structlog.get_logger(__name__)


class CooldownGate:
    """종류별 쿨다운 판정기."""

    def __init__(self, alert_dir: Path) -> None:
        self._dir = alert_dir

    def path_for(self, kind: str) -> Path:
        # kind 는 코드 상수에서만 오지만, 경로 조작으로 번지지 않게 막아 둔다.
        safe = kind.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe}.cooldown"

    def should_send(self, kind: str, cooldown_sec: int) -> bool:
        """이 종류를 지금 보내도 되는가."""
        if cooldown_sec <= 0:
            return True
        path = self.path_for(kind)
        try:
            last_ms = int(path.stat().st_mtime * 1000)
        except FileNotFoundError:
            return True
        except OSError as exc:
            # 판정 불가 → 보낸다. 침묵보다 중복이 낫다.
            log.warning("notify.cooldown_stat_failed", kind=kind, error=str(exc))
            return True
        return (now_ms() - last_ms) >= cooldown_sec * 1000

    def stamp(self, kind: str) -> None:
        """방금 보냈음을 기록한다."""
        path = self.path_for(kind)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(epoch_ms_to_iso(now_ms()), encoding="utf-8")
        except OSError as exc:
            # 기록 실패는 "쿨다운이 안 걸린다"로 끝난다 — 수집을 멈출 이유는 없다.
            log.warning("notify.cooldown_write_failed", kind=kind, error=str(exc))

    def clear(self, kind: str) -> None:
        """쿨다운을 푼다 (사고가 해소됐을 때)."""
        with contextlib.suppress(OSError):
            self.path_for(kind).unlink(missing_ok=True)
