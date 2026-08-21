"""무엇/왜
재생 한 건이 취향에 대해 말해 주는 양을 숫자 하나로 바꾼다 (0층).

`audio-features` 도 `artist.genres` 도 API 에서 사라진 뒤, 취향을 알 수 있는
남은 근거는 **행동**뿐이다. 얼마나 끝까지 들었나, 어떻게 시작했나, 내가 고른
맥락이었나, 얼마나 최근인가.

설계 원칙
- **순수 함수.** DB·벽시계를 모른다. 파라미터와 값만 받아 계산한다. 그래야
  가중치 규칙을 표로 고정해 두고 시험할 수 있다 (`sessions/machine.py` 와
  같은 이유).
- **모드는 가산점이 아니라 기저율 대비 정규화다.** 2026-08-16 실측: 직접
  고른 `clickrow` 의 평균 완주율이 0.40, 흘러온 `trackdone` 이 0.84 다.
  "직접 골랐으니 가산점" 같은 규칙은 정확히 거꾸로 계산한다. 같은 80% 완주도
  clickrow 면 기저를 크게 웃돌고 trackdone 이면 거의 정보가 없다.
- **반감기가 둘이다.** 취향 감쇠가 단일 지수가 아니다 — 1~3개월에 급락한 뒤
  12~24개월이 평평하다. 빠르게 도는 회전층(`current`)과 몇 년 남는
  기저층(`core`)을 따로 만든다.
- **음수를 허용한다.** 즉시 이탈이 전체의 19% 다. 이걸 0 으로 뭉개면 "싫어함"
  이라는 정보를 통째로 버리게 된다. 다만 직접 골라 놓고 끈 경우만 강하게
  본다 — 흘러오다 끈 것은 딴짓일 수 있다.

어떻게 확인
    uv run pytest tests/test_features.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

__all__ = ["DEFAULT_PARAMS", "FeatureParams", "PlayWeight", "compute_weight"]

DAY_MS = 86_400_000


@dataclass(frozen=True)
class FeatureParams:
    """가중치 계산의 모든 손잡이. 버전과 함께 DB 에 굳혀 둔다.

    `mode_base_rates` 는 손으로 정한 값이 아니라 **데이터에서 유도한 값**인데,
    그래도 여기 함께 박아 둔다. 기저율은 데이터가 쌓이면 움직이고, 그때마다
    과거 점수의 의미가 소급해 바뀌면 재현이 불가능해지기 때문이다.
    """

    half_life_days_current: float = 182.0  # 6개월
    half_life_days_core: float = 730.0  # 24개월

    #: 절대 완주도와 기저 대비 상대값을 섞는 비율. 둘 다 필요하다 —
    #: 상대값만 쓰면 "끝까지 들었다"는 사실 자체가 사라진다.
    absolute_share: float = 0.5
    relative_share: float = 0.5

    #: 이보다 덜 들었으면 이탈로 본다 (실측 분포에서 <5% 가 뚜렷한 봉우리).
    abandon_threshold: float = 0.05
    abandon_penalty: float = 0.5
    #: 직접 고른 뒤의 이탈은 더 강한 거부 신호다.
    deliberate_abandon_multiplier: float = 2.0
    deliberate_modes: tuple[str, ...] = ("clickrow", "playbtn")

    #: context_origin 별 계수. unknown/external 은 중립(1.0).
    voluntary_self: float = 1.2
    voluntary_algorithmic: float = 0.6

    #: 모드별 평균 완주율. 비어 있으면 global_base_rate 로 떨어진다.
    mode_base_rates: dict[str, float] = field(default_factory=dict)
    global_base_rate: float = 0.7

    def base_rate(self, mode: str | None) -> float:
        if mode is None:
            return self.global_base_rate
        return self.mode_base_rates.get(mode, self.global_base_rate)


DEFAULT_PARAMS = FeatureParams()


@dataclass(frozen=True)
class PlayWeight:
    """한 재생의 계산 결과. `play_weight` 행이 되는 값들이다."""

    completion: float | None
    completion_source: str
    mode: str | None
    mode_score: float | None
    voluntary: float
    weight_current: float
    weight_core: float


def _decay(age_ms: int, half_life_days: float) -> float:
    """반감기 지수감쇠. 미래 시각(음수 나이)은 1.0 으로 막는다."""
    if half_life_days <= 0:
        return 1.0
    age_days = max(0.0, age_ms / DAY_MS)
    return math.exp(-math.log(2) * age_days / half_life_days)


def resolve_completion(
    *,
    measured: float | None,
    ms_played: int | None,
    duration_ms: int | None,
    inferred: float | None,
) -> tuple[float | None, str]:
    """완주도와 그 **출처**를 함께 돌려준다.

    규칙 3(실측과 추정을 섞지 않는다)을 지키는 지점이다. 하나의 숫자로
    뭉개면 나중에 "실측만으로 다시 계산"이 불가능해진다.

    우선순위는 폴러 실측 > export 가 준 ms_played > RP 추정.
    """
    if measured is not None:
        return _clamp(measured), "measured"
    if ms_played is not None and duration_ms:
        # 1 을 넘는 경우가 실재한다 (한 export 행 안에서 반복 재생, 최대 4.3배).
        return _clamp(ms_played / duration_ms), "export"
    if inferred is not None:
        return _clamp(inferred), "inferred"
    return None, "none"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_weight(
    *,
    completion: float | None,
    completion_source: str,
    mode: str | None,
    context_origin: str | None,
    age_ms: int,
    params: FeatureParams = DEFAULT_PARAMS,
) -> PlayWeight:
    """재생 한 건의 가중치.

    완주도를 모르면(`None`) 취향에 대해 아무 말도 하지 않는다 — 0 을 주는
    것과 다르다. 0 은 "안 들었다"이고 여기서는 "모른다"이기 때문이다.
    """
    voluntary = _voluntary_coefficient(context_origin, params)
    if completion is None:
        return PlayWeight(
            completion=None,
            completion_source=completion_source,
            mode=mode,
            mode_score=None,
            voluntary=voluntary,
            weight_current=0.0,
            weight_core=0.0,
        )

    base_rate = params.base_rate(mode)
    mode_score = completion - base_rate
    base = params.absolute_share * completion + params.relative_share * mode_score

    if completion < params.abandon_threshold:
        penalty = params.abandon_penalty
        if mode in params.deliberate_modes:
            penalty *= params.deliberate_abandon_multiplier
        base -= penalty

    signal = base * voluntary
    return PlayWeight(
        completion=completion,
        completion_source=completion_source,
        mode=mode,
        mode_score=mode_score,
        voluntary=voluntary,
        weight_current=signal * _decay(age_ms, params.half_life_days_current),
        weight_core=signal * _decay(age_ms, params.half_life_days_core),
    )


def _voluntary_coefficient(context_origin: str | None, params: FeatureParams) -> float:
    """맥락의 자발성. 모르면 중립이다 — 추측하지 않는다."""
    if context_origin == "self":
        return params.voluntary_self
    if context_origin == "algorithmic":
        return params.voluntary_algorithmic
    return 1.0
