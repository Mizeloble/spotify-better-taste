"""무엇/왜
분류 규칙의 버전과 알고리즘 플레이리스트 이름 패턴.

설계 원칙
- **`RULES_VERSION` 은 손으로 올린다.** 결정표·패턴 목록·성능저하 플래그 중
  무엇이든 바뀌면 올린다. 각 세션 행이 자기를 분류한 버전을 들고 있으므로
  `classify run --stale` 이 바뀐 것만 다시 계산한다. 이게 없으면 어떤 행이
  어떤 규칙으로 분류됐는지 영영 모르고, Phase 5 의 베이스라인 비교가
  재현 불가능해진다.
- 이름 패턴은 **폴백일 뿐**이다. 소유권 판정을 절대 이기지 못한다 —
  내가 만든 "Summer Mix" 는 self 다.
- 계정이 한국 로케일이라 EN/KO 를 함께 둔다.

어떻게 확인
    uv run pytest tests/test_classifier.py
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["ALGORITHMIC_NAME_PATTERNS", "RULES_VERSION", "matches_algorithmic_name"]

#: 결정표·패턴·성능저하 플래그가 바뀌면 올린다.
RULES_VERSION: Final = 1

#: Spotify 가 만든 알고리즘 플레이리스트의 이름들.
#: owner 를 확인할 수 없거나, spotify 소유인데 이름이 이상한 경우의 폴백.
ALGORITHMIC_NAME_PATTERNS: Final[tuple[str, ...]] = (
    r"^Discover Weekly$",
    r"^새로운 발견$",
    r"^Release Radar$",
    r"^릴리스 레이더$",
    r"^Daily Mix \d+$",
    r"^데일리 믹스 \d+$",
    r"^On Repeat$",
    r"^자주 듣는 음악$",
    r"^Repeat Rewind$",
    r"^다시 듣는 음악$",
    r"^Time Capsule$",
    r"^타임캡슐$",
    r"^Your Top Songs \d{4}$",
    r"^올해의 톱 \d{4}$",
    r"^.+ Mix$",
    r"^.+ 믹스$",
    r"^.+ Radio$",
    r"^.+ 라디오$",
    r"^.+ Blend$",
    r"^Made For You",
    r"Sonic Fingerprint",
    r"Wrapped",
)

_COMPILED: Final = tuple(re.compile(p, re.IGNORECASE) for p in ALGORITHMIC_NAME_PATTERNS)


def matches_algorithmic_name(name: str | None) -> str | None:
    """이름이 알고리즘 패턴에 맞으면 그 패턴을, 아니면 None."""
    if not name:
        return None
    for pattern in _COMPILED:
        if pattern.search(name):
            return pattern.pattern
    return None
