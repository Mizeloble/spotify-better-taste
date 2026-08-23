"""무엇/왜
행동 프로파일 (3층). "이 사람은 어떻게 듣는가"를 기간별 지표로 요약한다.

`audio-features` 도 `artist.genres` 도 사라진 뒤 남은 근거가 행동뿐인데,
0·1층은 그 행동을 **엔티티별 점수**로 접는다. 접기 전의 모양 — 얼마나 끝까지
듣나, 얼마나 되풀이하나, 새 곡을 얼마나 여나, 몇 시에 무엇이 달라지나 — 는
그 자체로 취향의 축이고 장르가 없어도 만들 수 있다. 이 층이 2층(장르)보다
먼저 나온 이유다.

설계 원칙
- **순수 함수.** DB·벽시계·시간대를 모른다. KST 시/요일은 이미 정수로 계산돼
  들어온다 (`weights.py` 와 같은 이유 — 표로 고정해 두고 시험할 수 있어야 한다).
- **분모를 숨기지 않는다.** 모든 지표가 `sample_size` 와 함께 나간다. 03시의
  완주율 0.95 가 4건짜리인지 400건짜리인지 모르면 읽으나 마나다.
- **모르는 것은 분모에서 뺀다.** `voluntary_rate` 의 분모는 분류된 재생뿐이다.
  unknown 을 "자발이 아님"으로 세면 null context 를 algorithmic 으로 추측하는
  것과 같은 잘못이 된다 (규칙: null context 는 unknown 이다).
- **'처음 듣는 곡'은 전 기간에서 한 번만 정해진다.** `measured` 로 걸러 놓고
  그 안에서 처음을 찾으면 "실측된 것 중 처음"이 되어 뜻이 달라진다. 첫 재생
  판정은 항상 전체 이력에서 하고, 필터는 그 뒤에 건다.

한계 (읽는 사람이 반드시 알아야 하는 것)
- `exploration_rate` 는 `source_filter='all'` 만 믿을 수 있다. `measured` 는
  완주도를 아는 재생만 남기는데 그건 곧 백필된 트랙이고, 백필은 재생 횟수
  순이라 **롱테일이 통째로 빠진다** — 탐색이 일어나는 바로 그 자리다.
- `repeat_rate` 는 기간이 길수록 커진다. 기간이 다른 값끼리 비교하지 말 것.

어떻게 확인
    uv run pytest tests/test_behavior.py
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

__all__ = [
    "ALL_PERIOD",
    "BehaviorInput",
    "BehaviorMetric",
    "compute_behavior_profile",
]

#: 전 구간을 뜻하는 `period` 값. 시간대는 `hour:07`, 요일은 `dow:0`(일요일).
ALL_PERIOD = "all"

#: 실측만 남긴 벌. 완주도 출처가 폴러인 재생.
SOURCE_MEASURED = "measured"
SOURCE_ALL = "all"


@dataclass(frozen=True)
class BehaviorInput:
    """재생 한 건. `started_at` 오름차순으로 들어와야 한다.

    순서가 곧 '처음 듣는 곡' 판정의 근거다 — 뒤섞여 들어오면 탐색률이 조용히
    틀린 값을 낸다. 그래서 정렬은 호출부가 SQL 로 보장하고, 여기서 다시
    정렬하지 않는다 (정렬을 숨기면 호출부가 안심하고 틀린 순서를 넘긴다).
    """

    track_uri: str
    completion: float | None
    completion_source: str
    context_origin: str | None
    #: KST 기준. 변환은 호출부(표시 경계)에서 이미 끝났다.
    hour: int
    dow: int


@dataclass(frozen=True)
class BehaviorMetric:
    """`behavior_profile` 한 행이 되는 값."""

    period: str
    metric: str
    source_filter: str
    value: float
    sample_size: int


@dataclass
class _Bucket:
    """한 (기간, 필터) 조합의 누적기."""

    plays: int = 0
    unique_tracks: set[str] = field(default_factory=set)
    first_plays: int = 0
    completions: list[float] = field(default_factory=list)
    abandons: int = 0
    self_plays: int = 0
    algorithmic_plays: int = 0

    def add(self, row: BehaviorInput, *, is_first_play: bool, abandon_threshold: float) -> None:
        self.plays += 1
        self.unique_tracks.add(row.track_uri)
        if is_first_play:
            self.first_plays += 1
        if row.completion is not None:
            self.completions.append(row.completion)
            if row.completion < abandon_threshold:
                self.abandons += 1
        if row.context_origin == "self":
            self.self_plays += 1
        elif row.context_origin == "algorithmic":
            self.algorithmic_plays += 1


def compute_behavior_profile(
    rows: Iterable[BehaviorInput],
    *,
    abandon_threshold: float,
) -> list[BehaviorMetric]:
    """행동 지표를 전 구간·시간대·요일별로 계산한다.

    `rows` 는 `started_at` 오름차순이어야 한다 (BehaviorInput 참조).
    분모가 0 인 지표는 **내보내지 않는다** — 0.0 으로 채우면 "없음"과
    "0 이다"를 구별할 수 없게 된다.
    """
    buckets: dict[tuple[str, str], _Bucket] = {}
    seen: set[str] = set()
    total_plays = {SOURCE_ALL: 0, SOURCE_MEASURED: 0}

    for row in rows:
        # 첫 재생 판정은 필터보다 먼저다. 여기서 걸러 놓고 세면 "실측된 것
        # 중 처음"이 되어 완전히 다른 값이 된다.
        is_first_play = row.track_uri not in seen
        seen.add(row.track_uri)

        filters = [SOURCE_ALL]
        if row.completion_source == SOURCE_MEASURED:
            filters.append(SOURCE_MEASURED)

        for source_filter in filters:
            total_plays[source_filter] += 1
            for period in (ALL_PERIOD, f"hour:{row.hour:02d}", f"dow:{row.dow}"):
                bucket = buckets.setdefault((period, source_filter), _Bucket())
                bucket.add(row, is_first_play=is_first_play, abandon_threshold=abandon_threshold)

    metrics: list[BehaviorMetric] = []
    for (period, source_filter), bucket in sorted(buckets.items()):
        metrics.extend(
            _metrics_for(period, source_filter, bucket, total=total_plays[source_filter])
        )
    return metrics


def _metrics_for(
    period: str, source_filter: str, bucket: _Bucket, *, total: int
) -> list[BehaviorMetric]:
    out: list[BehaviorMetric] = []

    def emit(metric: str, value: float | None, sample_size: int) -> None:
        if value is None or sample_size <= 0:
            return
        out.append(BehaviorMetric(period, metric, source_filter, float(value), sample_size))

    n = bucket.plays
    emit("play_count", n, n)
    if period != ALL_PERIOD:
        # 이 기간이 전체 재생에서 차지하는 몫. 시간대 패턴은 완주율보다
        # 이쪽이 먼저 눈에 들어온다.
        emit("play_share", n / total if total else None, n)

    completions = bucket.completions
    emit("completion_mean", _mean(completions), len(completions))
    emit("completion_p50", _percentile(completions, 0.50), len(completions))
    emit("completion_p25", _percentile(completions, 0.25), len(completions))
    emit("completion_p75", _percentile(completions, 0.75), len(completions))
    # 분모는 완주도를 아는 재생뿐이다. 모르는 재생을 '안 끊었다'로 세면
    # 백필이 덜 된 만큼 스킵률이 낮게 나온다.
    emit("skip_rate", bucket.abandons / len(completions) if completions else None, len(completions))

    emit("repeat_rate", 1.0 - len(bucket.unique_tracks) / n if n else None, n)
    emit("exploration_rate", bucket.first_plays / n if n else None, n)

    classified = bucket.self_plays + bucket.algorithmic_plays
    emit("voluntary_rate", bucket.self_plays / classified if classified else None, classified)
    # 위 비율을 얼마나 믿을지 알려면 분류가 얼마나 됐는지도 있어야 한다.
    emit("classified_rate", classified / n if n else None, n)
    return out


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _percentile(values: Sequence[float], q: float) -> float | None:
    """선형 보간 없는 최근접 순위 백분위수.

    보간하면 실제로 관측되지 않은 완주도가 지표로 올라온다. 분포를 눈으로
    보려는 것이므로 관측값 중 하나를 그대로 돌려주는 편이 정직하다.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]
