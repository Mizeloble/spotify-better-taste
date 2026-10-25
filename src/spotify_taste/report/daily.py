"""무엇/왜
수집 건강 리포트. 48시간 소크 테스트의 판정 근거이자, 이후 매일 아침
"어제 잘 돌았나"를 한 줄로 답하는 것.

설계 원칙
- **판정에 필요한 것만 담는다.** 세션 수, null-context 비율, unknown 비율,
  수집 공백 분, 백필 큐 깊이. 이 다섯이 계획서가 정한 성공 기준이다.
- 경고는 숫자와 함께 **무엇을 하라는지** 적는다. "unknown 이 높습니다"만으로는
  아무도 움직이지 않는다.
- **Mac 슬립은 정상 운영이다** (2026-10-18 결정, CLAUDE.md "수집 운영 방침").
  실시간은 recently-played 가, 완주·스킵은 주기적인 export 가 맡는다. 그래서
  "폴러 세션 0" 은 슬립으로 설명되면 경고가 아니고, 대신 export 가 얼마나
  낡았는지를 보여 주고 다시 요청할 때를 알린다.

어떻게 확인
    uv run spotify-taste report daily
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import structlog

from .._common import epoch_ms_to_kst, now_ms
from ..db.queries import (
    SELECT_BACKFILL_STATUS,
    SELECT_EXPORT_COVERAGE,
    SELECT_GAP_MINUTES_BY_REASON,
)

__all__ = ["DailyReport", "build_daily_report"]

log = structlog.get_logger(__name__)

DAY_MS = 86_400_000
#: 이 비율을 넘는 null context 는 계정 증상이다.
NULL_CONTEXT_ALARM = 0.80
#: 하루 이만큼 넘게 비면 수집에 문제가 있다.
GAP_MINUTES_ALARM = 120
#: 창의 이 비율 이상을 잤다면 폴러 세션 0 은 슬립으로 설명된다.
SLEEP_EXPLAINS_POLLER_SHARE = 0.5
#: export 커버리지가 이보다 낡으면 새로 요청할 때다. 한 달 주기로 요청하고
#: Spotify 가 보내 주기까지 최대 30일이 걸리므로, 30일에 여유 15일을 더했다.
EXPORT_STALE_DAYS = 45


@dataclass
class DailyReport:
    window_days: int
    sessions: int = 0
    poller_sessions: int = 0
    rp_sessions: int = 0
    samples: int = 0
    playing_samples: int = 0
    null_context_ratio: float = 0.0
    #: 분류를 **거쳤는데도** unknown 인 비율. 분모는 분류된 세션.
    unknown_ratio: float = 0.0
    #: 아직 classify 가 돌지 않은 세션 수 (04:40 배치 이후 생긴 세션).
    unclassified: int = 0
    gap_minutes: float = 0.0
    #: 사유별 공백 분. 합계만으로는 크래시인지 슬립인지 판단할 수 없다.
    gap_by_reason: dict[str, float] = field(default_factory=dict)
    backfill_pending: int = 0
    last_sample_at: int | None = None
    #: export 가 덮는 마지막 재생 종료 시각. 이 뒤로는 완주·스킵 실측이 없다.
    export_covered_until: int | None = None
    #: 그 뒤로 ms_played 없이 쌓인 RP 세션 — 다음 export 가 채울 몫.
    rp_awaiting_export: int = 0
    #: 집계 기준 시각. 경고 판정이 벽시계를 다시 읽지 않게 한다.
    as_of: int = 0
    warnings: list[str] = field(default_factory=list)
    #: 경고는 아니지만 사람이 알아야 할 맥락 (healthy 판정에 영향 없음).
    notes: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.warnings

    def lines(self) -> list[str]:
        """사람이 읽는 요약 (Telegram 본문으로도 쓴다)."""
        out = [
            f"세션        : {self.sessions:,} "
            f"(폴러 {self.poller_sessions:,} / RP {self.rp_sessions:,})",
            f"샘플        : {self.samples:,}",
            f"null context: {self.null_context_ratio:.1%}",
            f"unknown 출처: {self.unknown_ratio:.1%}",
        ]
        if self.unclassified:
            out.append(f"미분류      : {self.unclassified:,} (다음 classify 배치 대기)")
        out += [
            f"수집 공백   : {self.gap_minutes:.0f}분",
            f"백필 대기   : {self.backfill_pending:,}",
        ]
        if self.export_covered_until:
            out.append(
                f"완주 실측   : {epoch_ms_to_kst(self.export_covered_until):%m-%d} 까지 "
                f"(이후 RP {self.rp_awaiting_export:,}건 대기)"
            )
        else:
            out.append(f"완주 실측   : export 없음 (RP {self.rp_awaiting_export:,}건 대기)")
        if self.last_sample_at:
            out.append(f"마지막 샘플 : {epoch_ms_to_kst(self.last_sample_at):%m-%d %H:%M} KST")
        return out


def build_daily_report(conn: sqlite3.Connection, *, window_days: int = 1) -> DailyReport:
    """최근 N일의 수집 건강을 집계한다."""
    until = now_ms()
    since = until - window_days * DAY_MS
    report = DailyReport(window_days=window_days, as_of=until)

    row = conn.execute(
        """
        SELECT count(*) AS sessions,
               sum(CASE WHEN source = 'poller' THEN 1 ELSE 0 END) AS poller,
               sum(CASE WHEN source = 'recently_played' THEN 1 ELSE 0 END) AS rp,
               -- export 는 구조적으로 context 가 없으므로 null 비율에서 뺀다.
               sum(CASE WHEN context_uri IS NULL AND source != 'history_export'
                        THEN 1 ELSE 0 END) AS null_ctx,
               sum(CASE WHEN source != 'history_export' THEN 1 ELSE 0 END) AS ctx_capable,
               -- '분류했는데 unknown' 과 '아직 분류 안 됨(NULL)' 은 다른 문제다:
               -- 전자는 규칙 커버리지, 후자는 그냥 배치 대기 (2026-08-13 100% 오해).
               sum(CASE WHEN context_origin = 'unknown' THEN 1 ELSE 0 END) AS unknown_origin,
               sum(CASE WHEN context_origin IS NULL THEN 1 ELSE 0 END) AS unclassified
          FROM v_clean_session WHERE started_at >= ?
        """,
        (since,),
    ).fetchone()

    report.sessions = int(row["sessions"] or 0)
    report.poller_sessions = int(row["poller"] or 0)
    report.rp_sessions = int(row["rp"] or 0)
    report.unclassified = int(row["unclassified"] or 0)
    if report.sessions:
        ctx_capable = int(row["ctx_capable"] or 0)
        report.null_context_ratio = int(row["null_ctx"] or 0) / ctx_capable if ctx_capable else 0.0
        classified = report.sessions - report.unclassified
        report.unknown_ratio = int(row["unknown_origin"] or 0) / classified if classified else 0.0

    sample_row = conn.execute(
        """
        SELECT count(*) AS samples,
               sum(CASE WHEN is_playing = 1 THEN 1 ELSE 0 END) AS playing
          FROM player_sample WHERE sampled_at >= ?
        """,
        (since,),
    ).fetchone()
    report.samples = int(sample_row["samples"] or 0)
    report.playing_samples = int(sample_row["playing"] or 0)
    last = conn.execute("SELECT max(sampled_at) FROM player_sample").fetchone()[0]
    report.last_sample_at = int(last) if last is not None else None

    report.gap_by_reason = {
        str(r["reason"]): int(r["ms"] or 0) / 60_000
        for r in conn.execute(SELECT_GAP_MINUTES_BY_REASON, {"since": since, "until": until})
    }
    report.gap_minutes = sum(report.gap_by_reason.values())

    backfill = dict(conn.execute(SELECT_BACKFILL_STATUS).fetchone())
    report.backfill_pending = (
        backfill["tracks_pending"] + backfill["artists_pending"] + backfill["albums_pending"]
    )

    coverage = conn.execute(SELECT_EXPORT_COVERAGE).fetchone()
    if coverage["covered_until"] is not None:
        report.export_covered_until = int(coverage["covered_until"])
    report.rp_awaiting_export = int(coverage["rp_awaiting"] or 0)

    _add_warnings(report)
    # 세션 0 인 날에도 낸다 — 듣지 않은 날이라고 export 가 덜 낡는 건 아니다.
    _add_export_note(report)
    log.info(
        "report.daily",
        sessions=report.sessions,
        unknown_ratio=round(report.unknown_ratio, 3),
        unclassified=report.unclassified,
        gap_minutes=round(report.gap_minutes),
    )
    return report


def _add_warnings(report: DailyReport) -> None:
    """경고에는 반드시 '무엇을 하라'가 붙는다."""
    if report.sessions == 0:
        if report.samples and report.playing_samples == 0:
            # 폴러는 온종일 돌았는데 재생 중인 순간이 한 번도 없었다 — 장애가
            # 아니라 청취가 없었던 날이다. 여기서 경고를 내면 소크 테스트 내내
            # 헛경보가 쌓여 진짜 경보를 무디게 만든다 (2026-08-13 실제 오탐).
            report.notes.append(
                "세션 0 — 수집기는 정상입니다. 윈도 내 재생 중 샘플이 0개, "
                "즉 들은 곡이 없었던 것입니다."
            )
        else:
            report.warnings.append(
                "세션이 하나도 없습니다. `spotify-taste ops heartbeat-check` 로 수집기를 확인하세요."
            )
        return

    if report.null_context_ratio > NULL_CONTEXT_ALARM:
        report.warnings.append(
            f"null context 가 {report.null_context_ratio:.0%} 입니다. 사용자 행동이 아니라 "
            "계정 증상일 수 있습니다 — `classify report` 를 보고 "
            "CONTEXT_NULL_ACCOUNT=true 를 검토하세요."
        )
    # 랩탑이 잠든 시간은 수집기 잘못이 아니다 — 판정에서 뺀다 (2026-08-14 결정).
    # 표시 자체는 gap_minutes 에 그대로 남아 recently-played 조정 범위를 알려 준다.
    sleep_minutes = report.gap_by_reason.get("host_sleep", 0.0)
    judged_minutes = report.gap_minutes - sleep_minutes
    if sleep_minutes:
        report.notes.append(
            f"공백 {report.gap_minutes:.0f}분 중 {sleep_minutes:.0f}분은 호스트 슬립 — "
            "판정에서 제외했습니다."
        )
    if judged_minutes > GAP_MINUTES_ALARM:
        # 사유를 함께 적는다. process_restart 가 대부분이면 호스트가 꺼져
        # 있었을 수도, 수집기가 죽었을 수도 있다 — 둘은 로그에서만 갈린다.
        breakdown = ", ".join(
            f"{r} {m:.0f}분" for r, m in report.gap_by_reason.items() if r != "host_sleep"
        )
        report.warnings.append(
            f"수집 공백이 {judged_minutes:.0f}분입니다 ({breakdown}). "
            "`logs/collect-*.log` 에서 poller.tick_failed 와 '수집 시작' 시각을 "
            "확인하세요 — 호스트 슬립이면 수집기 문제가 아닙니다."
        )
    if report.poller_sessions == 0 and report.rp_sessions > 0:
        # 잠들어 있었다면 RP 만 들어오는 게 정상 운영이다. Mac 이 대부분
        # 깨어 있었는데도 폴러가 한 곡도 못 잡았다면 그건 수집기 문제다.
        window_minutes = report.window_days * 1440
        if sleep_minutes >= window_minutes * SLEEP_EXPLAINS_POLLER_SHARE:
            report.notes.append(
                "폴러 세션 0 — Mac 이 잠들어 있어 recently-played 로만 들어왔습니다. "
                "완주·스킵은 다음 export 가 채웁니다."
            )
        else:
            report.warnings.append(
                "폴러 세션이 0 입니다 — Mac 이 깨어 있었는데도 recently-played 로만 "
                "들어왔습니다. `spotify-taste ops heartbeat-check` 로 수집기를 확인하세요."
            )


def _add_export_note(report: DailyReport) -> None:
    """export 가 낡았으면 다시 요청하라고 알린다.

    경고가 아니라 참고다 — 요청하고 도착을 기다리는 몇 주 동안 매일 '주의' 가
    뜨면 진짜 경고가 묻힌다. healthy 판정에도 넣지 않는다.
    """
    if report.export_covered_until is None:
        days = None
    else:
        days = (report.as_of - report.export_covered_until) / DAY_MS
        if days <= EXPORT_STALE_DAYS:
            return
    gap = "export 가 없습니다" if days is None else f"export 가 {days:.0f}일 전까지만 덮습니다"
    report.notes.append(
        f"완주·스킵 실측: {gap} (RP {report.rp_awaiting_export:,}건 대기). "
        "Spotify 개인정보 페이지에서 Extended streaming history 를 요청하세요. "
        "이미 요청했다면 도착 후 data/import/extended-history/ 아래 날짜 폴더에 풀고 "
        "`spotify-taste import history --write`."
    )
