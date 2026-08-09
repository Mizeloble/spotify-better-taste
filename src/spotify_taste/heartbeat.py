"""무엇/왜
Dead-man switch. 수집기가 조용히 죽는 것을 5분 안에 알아채기 위한 장치.

설계 원칙
- **살아 있음 = "수집했다", 결코 "루프를 돌았다"가 아니다.** 토큰이 죽었거나
  DB 락에 걸린 프로세스도 루프는 계속 돈다. 그래서 `write()` 는 샘플 적재에
  성공한 뒤에만 불리고, 점검 쪽은 파일 mtime **과** `max(sampled_at)` 을
  함께 본다. 파일이 신선한데 데이터가 낡았으면 그건 살아 있는 게 아니다.
- 파일의 **mtime 이 신호**이고 내용은 사람이 읽을 때만 쓴다.
- `check_heartbeat` 는 순수 함수다. 시각을 주입받아 테스트가 30분을 기다리지
  않아도 되게 한다.
- 정상 종료 센티넬이 있으면 알리지 않는다. 사람이 일부러 멈춘 것까지
  알람으로 만들면 알람을 안 보게 된다.

어떻게 확인
    uv run spotify-taste ops heartbeat-check
    uv run pytest tests/test_heartbeat.py
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from ._common import epoch_ms_to_iso, now_ms

__all__ = ["Heartbeat", "HeartbeatStatus", "HeartbeatVerdict", "check_heartbeat"]

log = structlog.get_logger(__name__)

#: 이보다 오래 샘플이 없으면 죽은 것으로 본다. 유휴 백오프 상한(120초)의
#: 여러 배 — 정상적인 유휴를 장애로 오인하지 않을 만큼 넉넉해야 한다.
DEFAULT_MAX_AGE_SEC = 900


class HeartbeatStatus(StrEnum):
    OK = "ok"
    MISSING = "missing"  # 한 번도 쓰인 적 없음
    STALE = "stale"  # 오래됨 → 장애
    STOPPED = "stopped"  # 정상 종료 센티넬 있음


@dataclass(frozen=True)
class HeartbeatVerdict:
    status: HeartbeatStatus
    age_sec: float | None
    detail: str = ""

    @property
    def should_alert(self) -> bool:
        return self.status in (HeartbeatStatus.MISSING, HeartbeatStatus.STALE)


class Heartbeat:
    """수집기 쪽. 샘플 적재 성공 후에만 호출한다."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self) -> None:
        """지금 살아 있음을 기록한다. 실패해도 수집을 멈추지 않는다."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(epoch_ms_to_iso(now_ms()), encoding="utf-8")
        except OSError as exc:
            log.warning("heartbeat.write_failed", path=str(self._path), error=str(exc))

    def mark_stopped(self, sentinel: Path) -> None:
        """정상 종료 표식. 사람이 멈춘 것을 장애로 알리지 않기 위해."""
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(epoch_ms_to_iso(now_ms()), encoding="utf-8")
        except OSError as exc:
            log.warning("heartbeat.sentinel_write_failed", error=str(exc))

    def clear_stopped(self, sentinel: Path) -> None:
        """기동 시 호출. 지난번 정상 종료 표식을 지운다."""
        with contextlib.suppress(OSError):
            sentinel.unlink(missing_ok=True)


def check_heartbeat(
    *,
    path: Path,
    now_ms_value: int,
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
    stopped_sentinel: Path | None = None,
    last_sample_at: int | None = None,
) -> HeartbeatVerdict:
    """수집기가 살아 있는지 판정한다. 순수 함수.

    `last_sample_at` 을 주면 **데이터 기준으로도** 검사한다. 이게 핵심이다 —
    heartbeat 파일만 보면 "루프는 도는데 아무것도 수집하지 못하는" 상태를
    놓친다.
    """
    if stopped_sentinel is not None and stopped_sentinel.exists():
        return HeartbeatVerdict(HeartbeatStatus.STOPPED, None, "정상 종료 표식 있음")

    try:
        mtime_ms = int(path.stat().st_mtime * 1000)
    except FileNotFoundError:
        return HeartbeatVerdict(HeartbeatStatus.MISSING, None, f"{path} 가 없습니다")
    except OSError as exc:
        return HeartbeatVerdict(HeartbeatStatus.MISSING, None, f"{path} 확인 실패: {exc}")

    age_sec = (now_ms_value - mtime_ms) / 1000
    if age_sec > max_age_sec:
        return HeartbeatVerdict(
            HeartbeatStatus.STALE, age_sec, f"heartbeat 가 {age_sec / 60:.0f}분 전입니다"
        )

    # 파일은 신선한데 실제 수집이 멈춰 있는 경우 (C7).
    if last_sample_at is not None:
        sample_age = (now_ms_value - last_sample_at) / 1000
        if sample_age > max_age_sec:
            return HeartbeatVerdict(
                HeartbeatStatus.STALE,
                sample_age,
                f"프로세스는 살아 있지만 마지막 샘플이 {sample_age / 60:.0f}분 전입니다",
            )

    return HeartbeatVerdict(HeartbeatStatus.OK, age_sec)
