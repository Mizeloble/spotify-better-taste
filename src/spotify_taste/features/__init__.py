"""취향 특성 계산 (Phase 2). 0층 가중치 · 1층 친화도 · 3층 행동 프로파일."""

from .behavior import BehaviorInput, BehaviorMetric, compute_behavior_profile
from .build import (
    BehaviorResult,
    BuildResult,
    build_behavior_profile,
    build_features,
    create_params_version,
    load_params,
)
from .weights import DEFAULT_PARAMS, FeatureParams, PlayWeight, compute_weight

__all__ = [
    "DEFAULT_PARAMS",
    "BehaviorInput",
    "BehaviorMetric",
    "BehaviorResult",
    "BuildResult",
    "FeatureParams",
    "PlayWeight",
    "build_behavior_profile",
    "build_features",
    "compute_behavior_profile",
    "compute_weight",
    "create_params_version",
    "load_params",
]
