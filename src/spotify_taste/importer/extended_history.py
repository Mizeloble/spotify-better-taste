"""무엇/왜
Extended Streaming History(GDPR export) 임포터. 수년치 재생 기록을
`play_session` 으로 옮긴다 — `ms_played` 가 Spotify 실측이라 폴러급 신호가
과거로 소급된다.

설계 원칙
- **`ts` 는 재생이 *끝난* 시각이다** (export ReadMe 명시). 그래서
  `started_at = ts - ms_played` 로 역산한다. 이걸 놓치면 모든 세션이
  곡 길이만큼 밀리고, KST 일자·시간대 프로필이 통째로 어긋난다.
- **멱등이다.** `UNIQUE(track_uri, started_at)` + `DO NOTHING` 이라 같은
  export 를 두 번 임포트해도 행이 늘지 않는다. 나중에 더 긴 export 가 오면
  그냥 다시 돌리면 된다.
- **기존 데이터와의 겹침 규칙**: 폴러 세션이 이미 있으면 그쪽이 이긴다
  (진행 궤적까지 있으므로). RP 세션이 있으면 export 가 **대체**한다 —
  RP 에는 ms_played 가 없고 export 에는 있다. 행은 지우지 않고
  `superseded_by` 로 표시만 한다 (이 리포의 불변 규칙).
- **duration 을 모르므로 비율을 지어내지 않는다.** export 에는 곡 길이가
  없다. `completion_ratio` 는 NULL 로 두고, 메타 백필이 `track.duration_ms`
  를 채우면 분석 시점에 조인으로 계산한다. 대신 Spotify 자체 판정
  (`reason_end='trackdone'`, `skipped`)을 그대로 보존한다 — 그게 더 강한
  신호다.
- 컨텍스트(플레이리스트 등)는 export 에 아예 없다. NULL 로 두면 분류기의
  null-context 규칙(라이브러리 보유, 무음+셔플)이 처리한다.

어떻게 확인
    uv run spotify-taste import history            # dry-run
    uv run pytest tests/test_import_history.py
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .._common import iso_to_epoch_ms, now_ms
from ..db.queries import (
    MARK_SUPERSEDED,
    SELECT_POLLER_MATCH,
    SELECT_RP_NEAR,
)
from ..sessions.merge import MATCH_WINDOW_MS

__all__ = ["ImportStats", "find_audio_files", "import_extended_history"]

log = structlog.get_logger(__name__)

INSERT_EXPORT_SESSION = """
INSERT INTO play_session
  (track_uri, started_at, ended_at, ms_played,
   device_type, device_is_private, shuffle,
   source, timing_confidence, end_reason, reason_start, skipped,
   is_open, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, 'history_export', 'high', ?, ?, ?, 0, ?, ?)
ON CONFLICT (track_uri, started_at) DO NOTHING
"""


@dataclass
class ImportStats:
    files: int = 0
    read: int = 0
    imported: int = 0
    skipped_not_music: int = 0  # 팟캐스트·오디오북 (track_uri 없음)
    skipped_zero_ms: int = 0  # 재생 0ms — 큐 아티팩트/팬텀
    duplicates: int = 0  # 이미 임포트된 행 (멱등 재실행)
    poller_wins: int = 0  # 폴러 세션이 이미 설명하는 재생
    superseded_rp: int = 0  # export 가 대체한 RP 행
    offline_plays: int = 0  # ts 정확도가 낮을 수 있는 오프라인 재생
    first_ts: int | None = None
    last_ts: int | None = None
    #: 임포트된 고유 트랙 수 — 백필 큐 증가량의 예고편.
    distinct_tracks: set[str] = field(default_factory=set)

    def lines(self) -> list[str]:
        from .._common import epoch_ms_to_kst

        out = [
            f"파일        : {self.files}",
            f"레코드      : {self.read:,}",
            f"임포트      : {self.imported:,}",
            f"고유 트랙   : {len(self.distinct_tracks):,}",
            f"음악 아님   : {self.skipped_not_music:,} (팟캐스트/오디오북)",
            f"0ms 재생    : {self.skipped_zero_ms:,} (큐 아티팩트)",
            f"중복        : {self.duplicates:,}",
            f"폴러 우선   : {self.poller_wins:,}",
            f"RP 대체     : {self.superseded_rp:,}",
        ]
        if self.offline_plays:
            out.append(f"오프라인    : {self.offline_plays:,} (시각 정확도 낮음)")
        if self.first_ts and self.last_ts:
            out.append(
                f"기간        : {epoch_ms_to_kst(self.first_ts):%Y-%m-%d}"
                f" ~ {epoch_ms_to_kst(self.last_ts):%Y-%m-%d} KST"
            )
        return out


def find_audio_files(directory: Path) -> list[Path]:
    """Audio JSON 만 고른다. Video 는 음악 청취가 아니므로 제외한다."""
    return sorted(directory.rglob("Streaming_History_Audio_*.json"))


def import_extended_history(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    write: bool = False,
) -> ImportStats:
    """디렉터리의 Audio export 전부를 임포트한다.

    `write=False` 면 아무것도 저장하지 않고 통계만 만든다 (이 리포의 관례).
    """
    stats = ImportStats()
    files = find_audio_files(directory)
    stats.files = len(files)
    if not files:
        log.warning("import.no_files", directory=str(directory))
        return stats

    now = now_ms()
    for path in files:
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("import.file_unreadable", path=str(path), error=type(exc).__name__)
            continue
        if not isinstance(records, list):
            log.warning("import.unexpected_shape", path=str(path))
            continue
        for record in records:
            if isinstance(record, dict):
                _import_one(conn, record, stats, now=now, write=write)

    log.info(
        "import.done",
        imported=stats.imported,
        read=stats.read,
        duplicates=stats.duplicates,
        superseded_rp=stats.superseded_rp,
        write=write,
    )
    return stats


def _import_one(
    conn: sqlite3.Connection, r: dict, stats: ImportStats, *, now: int, write: bool
) -> None:
    stats.read += 1

    track_uri = r.get("spotify_track_uri")
    if not isinstance(track_uri, str) or not track_uri:
        stats.skipped_not_music += 1
        return

    ms_played = r.get("ms_played")
    if not isinstance(ms_played, int) or ms_played <= 0:
        # 0ms 는 큐 아티팩트다. 스킵 신호는 짧아도 ms_played > 0 으로 남는다.
        stats.skipped_zero_ms += 1
        return

    ts_raw = r.get("ts")
    if not isinstance(ts_raw, str):
        return
    try:
        ended_at = iso_to_epoch_ms(ts_raw)
    except ValueError:
        return

    # ts 는 재생이 *끝난* 시각이다 — 시작은 역산한다.
    started_at = ended_at - ms_played

    if r.get("offline"):
        # 오프라인 재생의 ts 는 동기화 시점일 수 있어 정확도가 낮다.
        # 버리지는 않는다 — 6년치에서 몇 시간 오차는 일자 프로필을 해치지 않는다.
        stats.offline_plays += 1

    stats.first_ts = started_at if stats.first_ts is None else min(stats.first_ts, started_at)
    stats.last_ts = ended_at if stats.last_ts is None else max(stats.last_ts, ended_at)
    stats.distinct_tracks.add(track_uri)

    # 폴러가 이미 이 재생을 설명하면 그쪽이 이긴다 (진행 궤적까지 있다).
    poller = conn.execute(
        SELECT_POLLER_MATCH, (track_uri, started_at, MATCH_WINDOW_MS, started_at)
    ).fetchone()
    if poller is not None:
        stats.poller_wins += 1
        return

    if not write:
        stats.imported += 1
        return

    cur = conn.execute(
        INSERT_EXPORT_SESSION,
        (
            track_uri,
            started_at,
            ended_at,
            ms_played,
            r.get("platform") if isinstance(r.get("platform"), str) else None,
            _bool(r.get("incognito_mode")),
            _bool(r.get("shuffle")),
            # Spotify 자체 판정을 원문 그대로 보존한다 (trackdone/fwdbtn/…).
            r.get("reason_end") if isinstance(r.get("reason_end"), str) else None,
            r.get("reason_start") if isinstance(r.get("reason_start"), str) else None,
            _bool(r.get("skipped")),
            now,
            now,
        ),
    )
    if cur.rowcount == 0:
        stats.duplicates += 1
        return
    export_id = int(cur.lastrowid or 0)
    stats.imported += 1

    # 같은 재생을 기록한 RP 행이 있으면 export 가 대체한다 — RP 에는
    # ms_played 가 없고 export 에는 있다. 지우지 않고 표시만 한다.
    near_rp = conn.execute(SELECT_RP_NEAR, (track_uri, started_at, MATCH_WINDOW_MS)).fetchone()
    if near_rp is not None and int(near_rp["id"]) != export_id:
        conn.execute(MARK_SUPERSEDED, (export_id, now, int(near_rp["id"])))
        stats.superseded_rp += 1


def _bool(value: object) -> int | None:
    return int(value) if isinstance(value, bool) else None
