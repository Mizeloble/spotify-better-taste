"""출처 분류. 재생이 self / algorithmic / external 중 무엇인지 판정한다."""

from .patterns import RULES_VERSION, matches_algorithmic_name
from .rules import ContextSource, Evidence, Origin, classify

__all__ = [
    "RULES_VERSION",
    "ContextSource",
    "Evidence",
    "Origin",
    "classify",
    "matches_algorithmic_name",
]
