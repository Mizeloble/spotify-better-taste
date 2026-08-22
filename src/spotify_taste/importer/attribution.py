"""무엇/왜
기술 로그의 셔플 시퀀스에서 재생의 **컨텍스트를 유추**한다. Extended Streaming
History 에는 컨텍스트가 아예 없어서(39,006행 전부 NULL) 지금 6개월치 재생의
절반 이상이 `unknown` 으로 남아 있는데, `ShuffleSequenceEvent` 에는
`context_uri` 와 그때 만들어진 트랙 순서가 통째로 들어 있다.

설계 원칙
- **순수 함수다.** DB·HTTP·벽시계 금지 (`sessions/machine.py` 와 같은 이유).
  임포트 경로와 재계산 경로가 같은 코드를 타야 한다.
- **유추라는 사실을 지우지 않는다.** 결과에 `confidence` 와 `lag_ms` 가 함께
  나오고, 저장도 `play_session.context_uri` 가 아니라 별도 증거 테이블에
  한다. 관측값과 섞이면 규칙 3 이 무너진다.
- **정확도는 84.5% 다** (2026-08-21, `TrackNotPlayed.play_context` 를 정답지로
  쓴 대조에서 49일치/9불일치, n=58). 폴러 관측과 겹치는 구간은 5일·3건뿐이라
  근거가 이 하나뿐이다. 이 숫자를 과신하지 말 것.
- **해석할 수 없는 컨텍스트는 타입을 NULL 로 둔다.** 지어내면 결정표가
  엉뚱한 분기를 타고, 그러면 이미 `self` 로 판정된 행이 `unknown` 으로
  퇴행한다 (정규화표 없이 돌렸을 때 349건 발생).

어떻게 확인
    uv run pytest tests/test_attribution.py
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

__all__ = [
    "CONTEXT_WINDOW_MS",
    "Attribution",
    "ContextSequence",
    "SequenceIndex",
    "attribute",
    "build_index",
    "normalize_context_type",
]

#: 이보다 오래된 시퀀스는 보지 않는다. 창을 1시간으로 좁혀도 커버리지가
#: 35.0% → 31.6% 로 3.4%p 만 줄어서, 넓게 잡되 confidence 로 가른다.
CONTEXT_WINDOW_MS = 6 * 60 * 60 * 1000
#: 시퀀스를 몇 개까지 거슬러 볼 것인가. 6개월간 819건뿐이라 넉넉한 값이다.
MAX_LOOKBACK_EVENTS = 8
#: lag 중앙값이 795초, p90 이 3,576초다. 그 분포에 맞춘 경계.
HIGH_LAG_MS = 10 * 60 * 1000
MEDIUM_LAG_MS = 60 * 60 * 1000


@dataclass(frozen=True)
class ContextSequence:
    """셔플 시퀀스 하나. `tracks` 는 그때 만들어진 재생 순서의 집합이다."""

    emitted_at: int
    context_uri: str
    tracks: frozenset[str]
    event_id: int | None = None


@dataclass(frozen=True)
class SequenceIndex:
    """정렬된 시퀀스와 그 시각 키. 수천 번 호출하므로 한 번만 만든다."""

    sequences: tuple[ContextSequence, ...]
    emitted_at: tuple[int, ...]


def build_index(sequences: list[ContextSequence]) -> SequenceIndex:
    ordered = tuple(sorted(sequences, key=lambda s: s.emitted_at))
    return SequenceIndex(ordered, tuple(s.emitted_at for s in ordered))


@dataclass(frozen=True)
class Attribution:
    context_uri: str
    #: 정규화 실패 시 None. 그대로 저장하고 결정표에는 넘기지 않는다.
    context_type: str | None
    confidence: str  # high|medium|low
    lag_ms: int
    event_id: int | None


def normalize_context_type(uri: str | None) -> str | None:
    """컨텍스트 URI 를 결정표가 아는 타입으로 옮긴다.

    **이 함수가 없으면 귀속이 아무 효과도 없다.** `classify()` 는
    `context_type` 으로 분기하는데 셔플 로그는 URI 만 주기 때문이다.
    """
    if not uri:
        return None
    parts = uri.split(":")
    if len(parts) < 2 or parts[0] != "spotify":
        return None
    kind = parts[1]
    if kind == "playlist":
        return "playlist"
    if kind == "user" and ":collection" in uri:
        # 좋아요 목록. `spotify:user:<me>:collection[:artist:<id>]` 두 형태가
        # 모두 온다. 계정마다 uri 가 다르므로 타입으로 판정한다.
        return "collection"
    if kind == "list" and ":artist_" in uri:
        # 아티스트 페이지의 발매 목록 (`popular-release-segments-…`).
        return "artist"
    if kind in ("album", "artist"):
        return kind
    if kind == "track":
        # 컨텍스트가 곡 자신이다 — 그 곡 하나를 직접 튼 재생.
        return "single_track"
    return None


def _confidence(lag_ms: int, *, shuffle: bool | None) -> str:
    if shuffle is not True:
        # 시퀀스는 셔플이 켜진 세션에서만 발행된다. 셔플이 꺼진 재생에 붙은
        # 증거는 낡았을 수 있다 (223건). 버리지는 않되 등급을 내린다.
        return "low"
    if lag_ms <= HIGH_LAG_MS:
        return "high"
    if lag_ms <= MEDIUM_LAG_MS:
        return "medium"
    return "low"


def attribute(
    track_uri: str,
    ended_at: int,
    index: SequenceIndex,
    *,
    shuffle: bool | None,
    window_ms: int = CONTEXT_WINDOW_MS,
) -> Attribution | None:
    """재생 하나에 컨텍스트를 붙인다. 못 붙이면 None."""
    if not index.sequences:
        return None
    start = bisect.bisect_right(index.emitted_at, ended_at) - 1
    for offset in range(start, max(-1, start - MAX_LOOKBACK_EVENTS), -1):
        if offset < 0:
            break
        seq = index.sequences[offset]
        lag = ended_at - seq.emitted_at
        if lag > window_ms:
            break
        if track_uri in seq.tracks:
            return Attribution(
                context_uri=seq.context_uri,
                context_type=normalize_context_type(seq.context_uri),
                confidence=_confidence(lag, shuffle=shuffle),
                lag_ms=lag,
                event_id=seq.event_id,
            )
    return None
