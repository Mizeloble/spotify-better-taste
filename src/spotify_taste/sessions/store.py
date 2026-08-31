"""무엇/왜
세션과 샘플을 DB 에 쓰고 읽는다. 상태머신을 순수하게 유지하려면 저장은
바깥에 있어야 하고, 그 바깥이 여기다.

설계 원칙
- **열린 세션도 매 틱 저장한다.** 별도 상태 파일을 두지 않는 이유: 파일과 DB 가
  어긋날 여지를 없애고, "지금 뭘 듣고 있지"가 공짜 쿼리가 되기 때문이다.
  열린 동안 `ended_at` 에는 마지막 샘플 시각이 들어가므로, 재시작 때
  "얼마나 오래된 반열림인가"를 바로 알 수 있다.
- **행을 지우지 않는다.** 재시작으로 버려지는 세션도 `end_reason` 을 남기고
  닫을 뿐이다.

어떻게 확인
    uv run pytest tests/test_session_restart.py
"""

from __future__ import annotations

import sqlite3

import structlog

from .._common import now_ms
from ..db.queries import (
    FINALIZE_OPEN_SESSION,
    INSERT_COLLECTOR_GAP,
    INSERT_PLAYER_SAMPLE,
    SELECT_LAST_SAMPLE_AT,
    SELECT_OPEN_SESSIONS,
    UPSERT_SESSION,
)
from .model import OpenSession, PlayerSample, PlaySession

__all__ = [
    "insert_gap",
    "insert_player_sample",
    "last_sample_at",
    "load_open_sessions",
    "upsert_closed_session",
    "upsert_open_session",
]

log = structlog.get_logger(__name__)


def insert_player_sample(
    conn: sqlite3.Connection, sample: PlayerSample, *, raw_id: int | None = None
) -> int:
    """샘플 한 건을 적재하고 행 id 를 돌려준다.

    이 테이블이 세션 재구성의 재현 가능한 원천이다 — 판정 규칙이 바뀌어도
    여기서 다시 계산할 수 있어야 한다.
    """
    cur = conn.execute(
        INSERT_PLAYER_SAMPLE,
        (
            sample.sampled_at,
            sample.http_status,
            _bool(sample.is_playing),
            sample.track_uri,
            sample.duration_ms,
            sample.progress_ms,
            sample.context_uri,
            sample.context_type,
            sample.device_id,
            sample.device_type,
            _bool(sample.device_is_private),
            _bool(sample.shuffle_state),
            sample.repeat_state,
            sample.currently_playing_type,
            sample.spotify_timestamp,
            raw_id,
        ),
    )
    return int(cur.lastrowid or 0)


def upsert_open_session(conn: sqlite3.Connection, o: OpenSession) -> None:
    """진행 중인 세션을 저장한다 (`is_open = 1`).

    매 틱 호출된다. 프로세스가 언제 죽어도 마지막 관측 상태가 남는다.
    """
    duration = o.duration_ms or None
    completion = min(1.0, o.max_progress_ms / duration) if duration else None
    listened = min(1.0, o.ms_played_accum / duration) if duration else None
    now = now_ms()
    conn.execute(
        UPSERT_SESSION,
        (
            o.track_uri,
            o.started_at,
            # 열린 동안 ended_at 은 '마지막으로 본 시각'이다.
            o.last_sample_at,
            o.ms_played_accum,
            o.duration_ms,
            o.max_progress_ms,
            completion,
            listened,
            o.context_uri,
            o.context_type,
            o.device_type,
            o.device_id,
            _bool(o.device_is_private),
            _bool(o.shuffle),
            "poller",
            "high",
            None,  # end_reason 은 닫힐 때 정해진다
            1,  # is_open
            o.sample_count,
            o.seek_forward_count,
            o.first_sample_id,
            o.last_sample_id,
            now,
            now,
        ),
    )


def upsert_closed_session(conn: sqlite3.Connection, s: PlaySession) -> None:
    """종료된 세션을 저장한다 (`is_open = 0`)."""
    now = now_ms()
    conn.execute(
        UPSERT_SESSION,
        (
            s.track_uri,
            s.started_at,
            s.ended_at,
            s.ms_played,
            s.duration_ms,
            s.max_progress_ms,
            s.completion_ratio,
            s.listened_ratio,
            s.context_uri,
            s.context_type,
            s.device_type,
            s.device_id,
            _bool(s.device_is_private),
            _bool(s.shuffle),
            s.source,
            s.timing_confidence,
            s.end_reason,
            0,  # is_open
            s.sample_count,
            s.seek_forward_count,
            s.first_sample_id,
            s.last_sample_id,
            now,
            now,
        ),
    )


def last_sample_at(conn: sqlite3.Connection) -> int | None:
    """마지막 샘플 시각. 한 번도 수집한 적 없으면 None.

    기동 시 "언제까지 살아 있었나"의 기준점이다. 열린 세션이 없어도 —
    아무것도 틀지 않는 동안에도 — 204 샘플은 계속 쌓이므로, 반열림 세션보다
    이쪽이 수집이 멈춘 순간을 정확히 짚는다.
    """
    row = conn.execute(SELECT_LAST_SAMPLE_AT).fetchone()
    value = row[0] if row is not None else None
    return int(value) if value is not None else None


def load_open_sessions(conn: sqlite3.Connection) -> list[tuple[int, OpenSession]]:
    """반열림 세션들을 (id, OpenSession) 으로 되살린다."""
    out: list[tuple[int, OpenSession]] = []
    for row in conn.execute(SELECT_OPEN_SESSIONS):
        out.append((int(row["id"]), _row_to_open(row)))
    return out


def finalize_open_session(conn: sqlite3.Connection, session_id: int, *, end_reason: str) -> None:
    """반열림 세션을 이어받지 않고 그대로 닫는다.

    지우지 않는 이유: 실제로 재생된 구간이고, `end_reason` 이 붙어 있으면
    분석에서 신뢰도를 낮춰 다루면 된다.
    """
    conn.execute(FINALIZE_OPEN_SESSION, (end_reason, now_ms(), session_id))


def insert_gap(conn: sqlite3.Connection, *, started_at: int, ended_at: int, reason: str) -> None:
    """수집 공백을 기록한다.

    recently-played 조정기가 "어디를 봐야 하는지" 를 추측하지 않게 하는 것이
    이 테이블의 목적이다.
    """
    if ended_at <= started_at:
        return
    conn.execute(INSERT_COLLECTOR_GAP, (started_at, ended_at, reason))
    log.info("poller.gap_recorded", started_at=started_at, ended_at=ended_at, reason=reason)


def _row_to_open(row: sqlite3.Row) -> OpenSession:
    last_seen = int(row["ended_at"] or row["started_at"])
    return OpenSession(
        track_uri=str(row["track_uri"]),
        started_at=int(row["started_at"]),
        duration_ms=row["duration_ms"],
        # 저장하지 않는 값이라 복원할 수 없다. 0 으로 두면 '중도 합류 아님'이
        # 되는데, 재시작 세션은 어차피 end_reason 으로 구분된다.
        first_progress_ms=0,
        last_progress_ms=int(row["max_progress_ms"] or 0),
        last_sample_at=last_seen,
        last_progress_change_at=last_seen,
        ms_played_accum=int(row["ms_played"] or 0),
        max_progress_ms=int(row["max_progress_ms"] or 0),
        context_uri=row["context_uri"],
        context_type=row["context_type"],
        device_id=row["device_id"],
        device_type=row["device_type"],
        device_is_private=_from_bool(row["device_is_private"]),
        shuffle=_from_bool(row["shuffle"]),
        sample_count=int(row["sample_count"] or 0),
        seek_forward_count=int(row["seek_forward_count"] or 0),
        first_sample_id=row["first_sample_id"],
        last_sample_id=row["last_sample_id"],
    )


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _from_bool(value: object) -> bool | None:
    return None if value is None else bool(value)
