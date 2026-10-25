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
- **Mac 이 잠든 시간은 나이에 넣지 않는다.** 슬립을 허용하는 운영(2026-10-18
  결정, CLAUDE.md "수집 운영 방침")에서는 깨어날 때마다 heartbeat 가 수십 분
  낡아 있다. 기준 시각을 `max(mtime, 마지막 기상)` 으로 잡아, 깨어난 뒤에도
  `max_age` 동안 수집하지 못했을 때만 알린다. 10-05~10-17 에 이 구분이 없어
  하루 ~10건, 총 130건의 헛경보가 나갔다.

어떻게 확인
    uv run spotify-taste ops heartbeat-check
    uv run pytest tests/test_heartbeat.py
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from ._common import epoch_ms_to_iso, now_ms

__all__ = [
    "Heartbeat",
    "HeartbeatStatus",
    "HeartbeatVerdict",
    "check_heartbeat",
    "read_last_wake_ms",
]

log = structlog.get_logger(__name__)

#: 이보다 오래 샘플이 없으면 죽은 것으로 본다. 유휴 백오프 상한(120초)의
#: 여러 배 — 정상적인 유휴를 장애로 오인하지 않을 만큼 넉넉해야 한다.
DEFAULT_MAX_AGE_SEC = 900

_WAKETIME_RE = re.compile(r"sec\s*=\s*(\d+)")


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
    last_wake_ms: int | None = None,
) -> HeartbeatVerdict:
    """수집기가 살아 있는지 판정한다. 순수 함수.

    `last_sample_at` 을 주면 **데이터 기준으로도** 검사한다. 이게 핵심이다 —
    heartbeat 파일만 보면 "루프는 도는데 아무것도 수집하지 못하는" 상태를
    놓친다.

    `last_wake_ms` 를 주면 그 이전의 슬립 시간은 나이에서 뺀다. 깨어난 지
    `max_age` 가 안 됐으면 수집기에게 아직 따라잡을 시간이 있는 것이다.
    """
    if stopped_sentinel is not None and stopped_sentinel.exists():
        return HeartbeatVerdict(HeartbeatStatus.STOPPED, None, "정상 종료 표식 있음")

    try:
        mtime_ms = int(path.stat().st_mtime * 1000)
    except FileNotFoundError:
        return HeartbeatVerdict(HeartbeatStatus.MISSING, None, f"{path} 가 없습니다")
    except OSError as exc:
        return HeartbeatVerdict(HeartbeatStatus.MISSING, None, f"{path} 확인 실패: {exc}")

    # 슬립 중에는 아무도 수집할 수 없다 — 나이는 깨어난 시각부터 센다.
    woke = last_wake_ms if last_wake_ms is not None and last_wake_ms <= now_ms_value else None
    since_wake = f" (깨어난 지 {(now_ms_value - woke) / 60_000:.0f}분)" if woke else ""

    age_sec = (now_ms_value - mtime_ms) / 1000
    if (now_ms_value - max(mtime_ms, woke or 0)) / 1000 > max_age_sec:
        return HeartbeatVerdict(
            HeartbeatStatus.STALE,
            age_sec,
            f"heartbeat 가 {age_sec / 60:.0f}분 전입니다{since_wake}",
        )

    # 파일은 신선한데 실제 수집이 멈춰 있는 경우 (C7).
    if last_sample_at is not None:
        sample_age = (now_ms_value - last_sample_at) / 1000
        if (now_ms_value - max(last_sample_at, woke or 0)) / 1000 > max_age_sec:
            return HeartbeatVerdict(
                HeartbeatStatus.STALE,
                sample_age,
                f"프로세스는 살아 있지만 마지막 샘플이 {sample_age / 60:.0f}분 전입니다"
                f"{since_wake}",
            )

    return HeartbeatVerdict(HeartbeatStatus.OK, age_sec)


def read_last_wake_ms() -> int | None:
    """macOS 의 마지막 기상 시각(`kern.waketime`). 모르면 None.

    DarkWake(Power Nap·유지보수 기상)도 기상으로 친다 — 그 몇 초 동안
    launchd 가 밀린 heartbeat-check 를 돌리므로, 헛경보가 바로 거기서 났다.
    부팅 후 한 번도 잠들지 않았으면 sec=0 이라 None 이다. 실패해도 판정을
    막지 않는다: None 이면 예전처럼 슬립을 구분하지 않고 판정한다.
    """
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.waketime"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    match = _WAKETIME_RE.search(out)
    if match is None or int(match.group(1)) == 0:
        return None
    return int(match.group(1)) * 1000
