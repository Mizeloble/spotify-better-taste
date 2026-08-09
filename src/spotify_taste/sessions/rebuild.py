"""무엇/왜
저장된 `player_sample` 을 다시 흘려 세션을 재계산한다. 판정 규칙을 바꿨을 때
과거 데이터를 새 규칙으로 다시 만드는 경로다.

설계 원칙
- **실시간 폴러와 같은 상태머신을 쓴다.** 다른 코드로 재구현하면 두 결과가
  갈라지고, 그 순간 "수집과 해석의 분리"는 말뿐이 된다.
  `test_session_rebuild.py` 가 두 출력이 같음을 단언한다.
- `--dry-run` 이 기본 사고방식이다. 무엇이 바뀌는지 먼저 보고 쓴다.

어떻게 확인
    uv run spotify-taste sessions rebuild --dry-run
    uv run pytest tests/test_session_rebuild.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import structlog

from ..db.queries import SELECT_SAMPLES_SINCE
from .machine import MachineConfig, SessionMachine
from .model import PlayerSample, PlaySession, SampleKind
from .store import upsert_closed_session

__all__ = ["RebuildResult", "load_samples", "rebuild_sessions"]

log = structlog.get_logger(__name__)


@dataclass
class RebuildResult:
    sample_count: int
    sessions: list[PlaySession]
    written: int = 0


def load_samples(
    conn: sqlite3.Connection, *, since: int = 0, until: int | None = None
) -> list[PlayerSample]:
    """저장된 샘플을 시간순으로 되살린다."""
    upper = until if until is not None else 2**62
    return [_row_to_sample(row) for row in conn.execute(SELECT_SAMPLES_SINCE, (since, upper))]


def rebuild_sessions(
    conn: sqlite3.Connection,
    *,
    since: int = 0,
    until: int | None = None,
    config: MachineConfig | None = None,
    write: bool = False,
) -> RebuildResult:
    """구간의 샘플을 재생해 세션을 만든다.

    `write=False` 면 아무것도 저장하지 않는다 — 규칙을 바꾸기 전에 결과를
    먼저 보는 것이 기본이다.
    """
    samples = load_samples(conn, since=since, until=until)
    machine = SessionMachine(config)
    sessions: list[PlaySession] = []
    for sample in samples:
        sessions.extend(machine.on_sample(sample))

    # 마지막에 열려 있는 세션은 **닫지 않는다.** 구간 끝에서 강제로 닫으면
    # 실시간 결과와 달라지고, 아직 진행 중인 재생을 잘라 버린다.
    result = RebuildResult(sample_count=len(samples), sessions=sessions)

    if write:
        for session in sessions:
            upsert_closed_session(conn, session)
        result.written = len(sessions)
    log.info(
        "sessions.rebuilt",
        samples=result.sample_count,
        sessions=len(sessions),
        written=result.written,
    )
    return result


def _row_to_sample(row: sqlite3.Row) -> PlayerSample:
    """`player_sample` 행 → `PlayerSample`.

    저장 당시 종류를 컬럼으로 남기지 않았으므로 http_status 와 track_uri 에서
    되살린다: 0 은 전송 실패, 204 나 트랙 없음은 유휴.
    """
    status = int(row["http_status"])
    track_uri = row["track_uri"]
    if status == 0:
        kind = SampleKind.ERROR
    elif status == 204 or track_uri is None:
        kind = SampleKind.IDLE
    else:
        kind = SampleKind.OK

    return PlayerSample(
        sampled_at=int(row["sampled_at"]),
        kind=kind,
        http_status=status,
        is_playing=_bool(row["is_playing"]),
        track_uri=track_uri,
        duration_ms=row["duration_ms"],
        progress_ms=row["progress_ms"],
        context_uri=row["context_uri"],
        context_type=row["context_type"],
        device_id=row["device_id"],
        device_type=row["device_type"],
        device_is_private=_bool(row["device_is_private"]),
        shuffle_state=_bool(row["shuffle_state"]),
        repeat_state=row["repeat_state"],
        currently_playing_type=row["currently_playing_type"],
        spotify_timestamp=row["spotify_timestamp"],
        id=int(row["id"]),
    )


def _bool(value: object) -> bool | None:
    return None if value is None else bool(value)
