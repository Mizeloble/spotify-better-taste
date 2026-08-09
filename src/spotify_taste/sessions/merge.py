"""무엇/왜
`recently-played` 항목을 기존 세션과 합친다. 폴러가 죽었거나 다른 기기에서
들은 구간을 메꾸되, **실측을 날조하지 않고** 중복을 만들지 않는 것이 목적이다.

설계 원칙
- **폴러가 항상 이긴다.** 타이밍·`ms_played`·기기·셔플 전부. RP 가 기여하는
  것은 "이 재생이 실제로 있었다"는 확인과, 폴러가 NULL 인 `context` 보충뿐이다.
  근거: RP 의 `played_at` 은 30초 단위로 반올림돼 오고 `ms_played` 가 아예 없다.
- **행을 지우지 않는다.** 중복은 `superseded_by`, 물리적으로 불가능한 겹침은
  `suspect_phantom` 으로 표시만 한다. 원본이 아카이브돼 있고 규칙은 언제든
  재계산 가능하므로, 삭제는 정보를 잃기만 한다.
- **추정을 실측 컬럼에 넣지 않는다.** RP 로 만든 세션은 `ms_played` 가 NULL 이고
  추정치는 `inferred_completion_ratio` 에만 들어간다.
- **`played_at` 의 의미를 추측하지 않고 측정한다.** 아래 보정 참조.

어떻게 확인
    uv run pytest tests/test_merge.py
"""

from __future__ import annotations

import sqlite3
import statistics

import structlog

from .._common import now_ms
from ..db.queries import (
    CONFIRM_SESSION_BY_RP,
    GET_CALIBRATION,
    INSERT_RP_SESSION,
    MARK_SUPERSEDED,
    SELECT_CALIBRATION_PAIRS,
    SELECT_POLLER_MATCH,
    SELECT_RP_NEAR,
    SET_CALIBRATION,
)
from ..spotify.schemas import RecentPlay

__all__ = [
    "CALIBRATION_KEY",
    "DEDUP_WINDOW_MS",
    "MATCH_WINDOW_MS",
    "MergeStats",
    "calibrate_played_at_offset",
    "infer_completion",
    "merge_recent_plays",
]

log = structlog.get_logger(__name__)

#: 폴러 세션과 같은 재생으로 볼 시간 창.
#: 30초 반올림 + 25초 폴링 입도 + 기기/서버 시계 오차를 모두 덮되, 같은 곡을
#: 진짜로 다시 튼 경우보다는 훨씬 짧아야 한다.
MATCH_WINDOW_MS = 90_000
#: 같은 트랙의 RP 항목이 이 안에 또 오면 중복으로 본다.
DEDUP_WINDOW_MS = 45_000

CALIBRATION_KEY = "played_at_offset_ms"
#: 이만큼 표본이 쌓이기 전에는 학습한 보정값을 믿지 않는다.
MIN_CALIBRATION_SAMPLES = 50


class MergeStats:
    """한 번의 병합 결과. 리포트와 테스트가 읽는다."""

    __slots__ = ("confirmed", "inserted", "skipped_duplicate", "superseded")

    def __init__(self) -> None:
        self.confirmed = 0
        self.inserted = 0
        self.skipped_duplicate = 0
        self.superseded = 0

    def __repr__(self) -> str:
        return (
            f"MergeStats(confirmed={self.confirmed}, inserted={self.inserted}, "
            f"skipped_duplicate={self.skipped_duplicate}, superseded={self.superseded})"
        )


def infer_completion(delta_ms: int | None, duration_ms: int | None) -> float | None:
    """다음 재생까지의 간격으로 완주 여부를 **거칠게** 추정한다.

    입력이 30초 해상도라 의도적으로 거칠다. 절반쯤 들은 경우는 복원이
    불가능하고, 아는 척하는 것이 모른다고 하는 것보다 나쁘다 — 그래서
    애매하면 None 이다.
    """
    if delta_ms is None or duration_ms is None or duration_ms <= 0:
        return None
    if delta_ms < 0:
        return None

    # 판정은 **비대칭**이다. 트랙보다 오래 걸려 다음 곡이 시작했다면 끝까지
    # 듣고 잠깐 쉰 것이고(트랙보다 길게 재생할 수는 없다), 트랙보다 짧으면
    # 넘긴 것이다. 절대 허용 오차(±60초)를 쓰면 짧은 곡에서 무너진다 —
    # 80초짜리를 40초 만에 넘긴 것도 |40-80|=40 <= 60 이라 '완주'가 된다.
    ratio = delta_ms / duration_ms
    if ratio >= 0.9:
        return 1.0
    # 아주 빨리 다음 곡으로 넘어갔다 → 스킵. 짧은 곡은 제외한다(구간이
    # 겹쳐 스킵과 완주를 가를 수 없다).
    if delta_ms <= 45_000 and duration_ms > 90_000:
        return round(ratio, 4)
    # 절반쯤 들은 경우는 30초 해상도에서 복원 불가능하다. 아는 척하지 않는다.
    return None


def get_offset_ms(conn: sqlite3.Connection) -> int:
    """학습된 `played_at` 보정값. 표본이 부족하면 0."""
    row = conn.execute(GET_CALIBRATION, (CALIBRATION_KEY,)).fetchone()
    if row is None:
        return 0
    if (row["sample_size"] or 0) < MIN_CALIBRATION_SAMPLES:
        return 0
    try:
        return int(float(row["value"]))
    except TypeError, ValueError:
        return 0


def calibrate_played_at_offset(conn: sqlite3.Connection, *, write: bool = False) -> tuple[int, int]:
    """`played_at` 이 재생 시작인지 종료인지를 **측정**한다.

    문서가 애매하므로 하드코딩하지 않는다. 두 수집기가 겹친 구간에서는
    폴러의 `started_at` 이 정답이므로, 확신 있게 매칭된 쌍의
    `median(rp.started_at - poller.started_at)` 을 본다.
    0 에 가까우면 시작 시각, +duration 에 가까우면 종료 시각이다.

    방향을 반대로 잡으면 갭 보충 세션 전부가 트랙 길이만큼 어긋나고,
    Phase 4 가 의존하는 시간대 특성이 조용히 오염된다.
    """
    deltas = [int(r["delta"]) for r in conn.execute(SELECT_CALIBRATION_PAIRS, (600_000,))]
    if not deltas:
        return 0, 0
    median = int(statistics.median(deltas))
    if write and len(deltas) >= MIN_CALIBRATION_SAMPLES:
        conn.execute(SET_CALIBRATION, (CALIBRATION_KEY, str(median), len(deltas), now_ms()))
        log.info("sessions.calibrated", offset_ms=median, samples=len(deltas))
    return median, len(deltas)


def merge_recent_plays(
    conn: sqlite3.Connection,
    plays: list[RecentPlay],
    *,
    offset_ms: int | None = None,
) -> MergeStats:
    """RP 항목들을 기존 세션과 합친다. `played_at` 오름차순으로 처리한다."""
    stats = MergeStats()
    if not plays:
        return stats

    offset = offset_ms if offset_ms is not None else get_offset_ms(conn)
    ordered = sorted(plays, key=lambda p: p.played_at)

    for index, play in enumerate(ordered):
        started_at = play.played_at + offset

        # 1) 폴러가 이미 이 재생을 설명하는가?
        match = conn.execute(
            SELECT_POLLER_MATCH,
            (play.track_uri, started_at, MATCH_WINDOW_MS, started_at),
        ).fetchone()
        if match is not None:
            conn.execute(
                CONFIRM_SESSION_BY_RP,
                (play.context_uri, play.context_type, now_ms(), int(match["id"])),
            )
            stats.confirmed += 1
            continue

        # 2) 근접 중복이거나 팬텀인가?
        #    재생하지 않은 곡이 중복 항목으로 들어오는 사례가 보고돼 있다.
        near = conn.execute(
            SELECT_RP_NEAR, (play.track_uri, started_at, DEDUP_WINDOW_MS)
        ).fetchone()
        if near is not None:
            stats.skipped_duplicate += 1
            log.debug("recent.near_duplicate_skipped", track_uri=play.track_uri)
            continue

        # 3) 갭 보충 세션을 넣는다. 실측은 NULL 로 남긴다.
        delta = _delta_to_next(ordered, index)
        inferred = infer_completion(delta, play.duration_ms)
        ended_at = started_at + min(
            delta if delta is not None else (play.duration_ms or 0),
            play.duration_ms or 0,
        )
        now = now_ms()
        conn.execute(
            INSERT_RP_SESSION,
            (
                play.track_uri,
                started_at,
                ended_at,
                play.duration_ms,
                inferred,
                play.context_uri,
                play.context_type,
                now,
                now,
            ),
        )
        stats.inserted += 1

    log.info(
        "recent.merged",
        confirmed=stats.confirmed,
        inserted=stats.inserted,
        skipped=stats.skipped_duplicate,
    )
    return stats


def supersede_rp_with_poller(
    conn: sqlite3.Connection, *, poller_session_id: int, track_uri: str, started_at: int
) -> int:
    """폴러 세션이 나중에 생겼을 때 같은 재생의 RP 행을 대체 표시한다.

    폴러가 재시작 후 emit 했는데 RP 가 이미 그 재생을 기록해 둔 경우.
    **지우지 않는다** — `v_clean_session` 이 걸러 주면 충분하다.
    """
    near = conn.execute(SELECT_RP_NEAR, (track_uri, started_at, MATCH_WINDOW_MS)).fetchone()
    if near is None:
        return 0
    conn.execute(MARK_SUPERSEDED, (poller_session_id, now_ms(), int(near["id"])))
    return 1


def _delta_to_next(ordered: list[RecentPlay], index: int) -> int | None:
    """다음 항목까지의 간격. 연속 재생이 아니면 None.

    10분 넘게 벌어져 있으면 '다음 곡'이 아니라 다른 청취 세션이므로,
    그 간격으로 완주를 추정하면 안 된다.
    """
    if index + 1 >= len(ordered):
        return None
    delta = ordered[index + 1].played_at - ordered[index].played_at
    return None if delta > 600_000 else delta
