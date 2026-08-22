"""무엇/왜
Technical Log Information(Spotify 개인정보 페이지) 임포터. 재생 *사실*을
늘리려는 것이 아니다 — `RawCoreStream` 4,146행 중 4,063행이 이미
`history_export` 로 들어와 있다. 이 export 로만 얻는 것은 셋이다:

1. **컨텍스트** — export 39,006행 전부가 `context_uri` NULL 이다.
   `ShuffleSequenceEvent` 로 6개월치의 35% 를 귀속한다.
2. **곡 길이** — `BoomboxPlaybackSession.ms_total_est`. 백필이 못 간 692곡을
   API 호출 0회로 얻는다.
3. **알고리즘 노출** — 무엇을 밀어 줬나. Extended History 에 원리적으로 없다.

설계 원칙
- **화이트리스트로만 읽는다.** `_FILES` 에 없는 파일은 존재해도 열지 않는다.
  같은 export 안에 평문 IP·이메일·전화번호가 들어 있기 때문이다.
- **사실 행을 늘리지 않는다.** 이미 그 재생을 설명하는 세션이 있으면 붙이기만
  한다(`techlog_stream.session_id`). 중복 세션은 모든 집계를 두 배로 샌다.
- **매칭 조건이 출처마다 다르다.** export 의 `ms_played` 는 기술 로그와 같은
  숫자지만 폴러는 독립 측정이라 최대 18초까지 벌어지고 RP 는 NULL 이다.
  하나의 규칙으로 통일하면 37건이 중복 삽입된다 (2026-08-21 실측).
- **유추를 관측과 섞지 않는다.** 귀속 결과는 `play_session.context_uri` 가
  아니라 `session_context_evidence` 로 간다 (규칙 3).
- **멱등하다.** `techlog_stream` 은 `(track_uri, ended_at)`, 나머지는 내용
  해시 `event_key` 로 막는다. NULL 이 섞인 컬럼 조합을 UNIQUE 로 쓰면 SQLite
  가 NULL 을 서로 다른 값으로 봐서 재임포트가 행을 두 배로 만든다.

어떻게 확인
    uv run spotify-taste import techlog --dir "~/Downloads/Spotify Technical Log Information"
    uv run pytest tests/test_import_techlog.py
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from .._common import iso_to_epoch_ms, now_ms, sha256_hex
from ..classify.patterns import RULES_VERSION
from ..db.queries import (
    INSERT_ALGO_EXPOSURE,
    INSERT_ALGO_EXPOSURE_ITEM,
    INSERT_CONTEXT_EVENT,
    INSERT_CONTEXT_ITEM,
    INSERT_IMPORT_BATCH,
    INSERT_TECHLOG_SESSION,
    INSERT_TECHLOG_STREAM,
    MARK_SUPERSEDED,
    SELECT_CONTEXT_EVENT_ID,
    SELECT_SESSIONS_NEAR,
    UPSERT_SESSION_CONTEXT_EVIDENCE,
    UPSERT_TRACK_DURATION_HINT,
)
from ..sessions.merge import MATCH_WINDOW_MS
from .attribution import ContextSequence, attribute, build_index

__all__ = ["TechlogStats", "discover_files", "import_techlog"]

log = structlog.get_logger(__name__)

#: 읽을 파일. 여기 없는 파일은 열지 않는다 (프라이버시 — 모듈 독스트링 참조).
_FILES: dict[str, str] = {
    "stream": "RawCoreStream.json",
    "boombox": "BoomboxPlaybackSession.json",
    "shuffle": "ShuffleSequenceEvent.json",
    "not_played": "TrackNotPlayed.json",
    "daylist": "DaylistGenerated.json",
    "on_repeat": "OnRepeatContents.json",
    "release_radar": "ReleaseRadarServedRecs.json",
    "continuum": "ContinuumSectionResponseV1.json",
}

#: `ms_played` 대조 허용치. export·techlog 행에만 적용한다.
#:
#: 시각만으로는 붙일 수 없다. `started_at` 이 출처마다 다른 이유로 어긋나기
#: 때문이다 (2026-08-21 실측): export 는 `ts` 가 초 단위로 절삭돼 중앙값
#: 492ms, 폴러는 25초 폴링에서 역산해 중앙값 12.6초(최대 78초), RP 는 30초
#: 반올림이라 중앙값 57초(최대 86초)다. 그래서 시각 창은 이 리포가 이미 쓰는
#: 90초(`sessions.merge.MATCH_WINDOW_MS`)로 두고, **export 만** ms 로 조인다.
#: export 의 ms 는 기술 로그와 같은 숫자라 사실상 정확한 키가 된다.
MS_TOLERANCE_MS = 2_000
#: Boombox 시각 조인 허용치. 실측 시차는 중앙값 45ms · p95 112ms 다.
#: 5초씩 열면 5초 내 후보가 둘 이상인 288건에서 오결합이 난다.
BOOMBOX_GAP_MS = 1_000
#: 들은 시간이 곡 길이를 이만큼 넘으면 조인이 잘못된 것이다.
BOOMBOX_OVERRUN_MS = 2_000

#: 붙일 후보가 여럿일 때의 우선순위. 앞이 이긴다.
_SOURCE_PRIORITY = ("poller", "history_export", "techlog", "recently_played")
#: `ms_played` 가 기술 로그와 같은 숫자인 출처. 폴러는 독립 측정이라 제외.
_MS_COMPARABLE = frozenset({"history_export", "techlog"})


@dataclass
class TechlogStats:
    files: int = 0
    stream_rows: int = 0
    skipped_not_audio: int = 0
    skipped_zero_ms: int = 0
    attached: Counter[str] = field(default_factory=Counter)
    inserted: int = 0
    superseded_rp: int = 0
    boombox_joined: int = 0
    boombox_rejected: int = 0
    context_events: int = 0
    context_items: int = 0
    evidence: Counter[str] = field(default_factory=Counter)
    evidence_untyped: int = 0
    duration_hints: int = 0
    duration_conflicts: int = 0
    exposures: int = 0
    exposure_items: int = 0
    first_ts: int | None = None
    last_ts: int | None = None

    def lines(self) -> list[str]:
        from .._common import epoch_ms_to_kst

        attached = sum(self.attached.values())
        out = [
            f"파일          : {self.files}",
            f"재생 로그     : {self.stream_rows:,} (음악 아님 {self.skipped_not_audio:,} 제외)",
            f"기존에 붙임   : {attached:,}  "
            + " ".join(f"{k}={v:,}" for k, v in self.attached.most_common()),
            f"신규 세션     : {self.inserted:,}",
            f"RP 대체       : {self.superseded_rp:,}",
            f"곡 길이 조인  : {self.boombox_joined:,} (기각 {self.boombox_rejected:,})",
            f"길이 힌트     : {self.duration_hints:,} (관측 불일치 {self.duration_conflicts:,})",
            f"컨텍스트 증거 : {self.context_events:,} 이벤트 / {self.context_items:,} 항목",
            f"귀속          : {sum(self.evidence.values()):,}  "
            + " ".join(f"{k}={v:,}" for k, v in self.evidence.most_common())
            + (f"  (타입 미상 {self.evidence_untyped:,})" if self.evidence_untyped else ""),
            f"알고리즘 노출 : {self.exposures:,} / {self.exposure_items:,} 항목",
        ]
        if self.first_ts and self.last_ts:
            out.append(
                f"기간          : {epoch_ms_to_kst(self.first_ts):%Y-%m-%d}"
                f" ~ {epoch_ms_to_kst(self.last_ts):%Y-%m-%d} KST"
            )
        return out


def discover_files(directory: Path) -> dict[str, Path]:
    """화이트리스트에 있는 파일만 찾는다. 없는 파일은 조용히 건너뛴다."""
    found: dict[str, Path] = {}
    for key, name in _FILES.items():
        path = directory / name
        if path.is_file():
            found[key] = path
    return found


def import_techlog(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    write: bool = False,
) -> TechlogStats:
    """기술 로그 export 를 임포트한다.

    `write=False` 면 아무것도 저장하지 않고 통계만 만든다 (이 리포의 관례).
    """
    stats = TechlogStats()
    files = discover_files(directory)
    stats.files = len(files)
    if "stream" not in files:
        log.warning("techlog.no_stream_file", directory=str(directory))
        return stats

    now = now_ms()
    streams = _read_streams(files["stream"], stats)
    if not streams:
        return stats
    stats.first_ts = min(s["ended_at"] for s in streams)
    stats.last_ts = max(s["ended_at"] for s in streams)

    _join_boombox(streams, files.get("boombox"), stats)

    batch_id = 0
    if write:
        cur = conn.execute(
            INSERT_IMPORT_BATCH,
            (
                "techlog",
                str(directory),
                stats.first_ts,
                stats.last_ts,
                json.dumps(_file_digests(files), ensure_ascii=False),
                None,
                now,
            ),
        )
        batch_id = int(cur.lastrowid or 0)

    sequences = _import_context_events(conn, files, stats, batch_id=batch_id, write=write)
    _link_sessions(conn, streams, stats, batch_id=batch_id, now=now, write=write)
    _write_evidence(conn, streams, sequences, stats, now=now, write=write)
    _write_duration_hints(conn, streams, stats, now=now, write=write)
    _import_exposures(conn, files, stats, batch_id=batch_id, write=write)

    log.info(
        "techlog.imported",
        stream_rows=stats.stream_rows,
        attached=sum(stats.attached.values()),
        inserted=stats.inserted,
        evidence=sum(stats.evidence.values()),
        write=write,
    )
    return stats


# ---------------------------------------------------------------------------
# 읽기
# ---------------------------------------------------------------------------


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("techlog.file_unreadable", path=str(path), error=type(exc).__name__)
        return []
    if not isinstance(data, list):
        log.warning("techlog.unexpected_shape", path=str(path))
        return []
    return [r for r in data if isinstance(r, dict)]


def _ts(record: dict[str, Any]) -> int | None:
    raw = record.get("timestamp_utc")
    if not isinstance(raw, str):
        return None
    try:
        return iso_to_epoch_ms(raw)
    except ValueError:
        return None


def _file_digests(files: dict[str, Path]) -> list[dict[str, Any]]:
    out = []
    for path in sorted(files.values()):
        data = path.read_bytes()
        out.append({"name": path.name, "sha256": sha256_hex(data), "bytes": len(data)})
    return out


def _bool(value: object) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _read_streams(path: Path, stats: TechlogStats) -> list[dict[str, Any]]:
    """`RawCoreStream` 을 해석 없이 옮긴다. 음악이 아닌 행만 걸러 낸다."""
    rows: list[dict[str, Any]] = []
    for r in _load(path):
        uri = r.get("message_content_uri")
        ended_at = _ts(r)
        ms = r.get("message_ms_played")
        if ended_at is None or not isinstance(ms, int):
            continue
        if r.get("message_media_type") != "audio" or not (
            isinstance(uri, str) and uri.startswith("spotify:track:")
        ):
            # 로컬 파일·팟캐스트·영상. `spotify:local::::` 은 식별자도 없다.
            stats.skipped_not_audio += 1
            continue
        rows.append(
            {
                "ended_at": ended_at,
                "track_uri": uri,
                "ms_played": ms,
                "reason_start": r.get("message_reason_start"),
                "reason_end": r.get("message_reason_end"),
                "shuffle": _bool(r.get("message_shuffle")),
                "media_type": r.get("message_media_type"),
                "is_downloaded": _bool(r.get("message_content_is_downloaded")),
                "social_listening": _bool(r.get("message_social_listening")),
                # 관계 연결 흔적. 기록만 하고 매칭에는 쓰지 않는다 — 다른 행
                # 185건을 대조하니 DB 는 전부 content_uri 쪽으로 갖고 있었다.
                "displayed_track_uri": r.get("message_displayed_content_uri") or None,
                "audio_id": r.get("message_audio_id"),
                "device_type": r.get("context_device_type"),
                "device_model": r.get("context_device_model"),
                "os_name": r.get("context_os_name"),
                "os_version": r.get("context_os_version"),
                "app_version": r.get("context_application_version"),
                "conn_country": r.get("context_conn_country"),
                "playback_id": None,
                "est_total_ms": None,
                "n_seekback": None,
                "ms_seekback": None,
                "n_seekfwd": None,
                "ms_seekfwd": None,
                "n_stutter": None,
                "boombox_gap_ms": None,
                "session_id": None,
            }
        )
    stats.stream_rows = len(rows)
    return rows


def _join_boombox(streams: list[dict[str, Any]], path: Path | None, stats: TechlogStats) -> None:
    """재생마다 가장 가까운 `BoomboxPlaybackSession` 을 붙인다.

    곡 길이(`ms_total_est`)를 여기서 얻는다. 이름과 달리 추정이 아니다 —
    이미 백필된 1,269곡과 대조해 오차 중앙값 0ms 였다 (2026-08-21).
    """
    if path is None:
        return
    records = []
    for r in _load(path):
        ts = _ts(r)
        if ts is not None:
            records.append((ts, r))
    records.sort(key=lambda z: z[0])
    keys = [z[0] for z in records]

    for s in streams:
        pos = bisect.bisect_left(keys, s["ended_at"])
        best: tuple[int, dict[str, Any]] | None = None
        for j in range(max(0, pos - 1), min(len(records), pos + 2)):
            gap = abs(records[j][0] - s["ended_at"])
            if best is None or gap < best[0]:
                best = (gap, records[j][1])
        if best is None or best[0] > BOOMBOX_GAP_MS:
            continue
        gap, rec = best
        total = rec.get("message_ms_total_est")
        if isinstance(total, int) and total > 0 and s["ms_played"] > total + BOOMBOX_OVERRUN_MS:
            # 들은 시간이 곡 길이보다 길다 — 다른 재생에 붙었다.
            stats.boombox_rejected += 1
            continue
        stats.boombox_joined += 1
        s["boombox_gap_ms"] = gap
        s["playback_id"] = rec.get("message_playback_id")
        s["est_total_ms"] = total if isinstance(total, int) and total > 0 else None
        s["n_seekback"] = rec.get("message_n_seekback")
        s["ms_seekback"] = rec.get("message_ms_seekback")
        s["n_seekfwd"] = rec.get("message_n_seekfwd")
        s["ms_seekfwd"] = rec.get("message_ms_seekfwd")
        s["n_stutter"] = rec.get("message_n_stutter")


# ---------------------------------------------------------------------------
# 세션 연결
# ---------------------------------------------------------------------------


def _pick_session(rows: list[sqlite3.Row], ms_played: int) -> sqlite3.Row | None:
    """붙일 세션을 고른다. 출처 우선순위 → 시각 근접 순.

    `ms_played` 조건은 export·techlog 행에만 건다. 폴러는 독립적으로 재기
    때문에 최대 18초까지 벌어지고(실측), RP 는 아예 NULL 이다. 전 출처에
    걸면 이미 있는 재생을 새 세션으로 또 넣게 된다.

    반대로 export 에는 ms 조건이 **반드시** 필요하다. 90초 창 안에 같은 곡의
    행이 둘 이상 있는 반복 재생 구간에서 시각만으로 고르면 엉뚱한 짝이 나온다
    (`ms_played` 0 ↔ 156,720 같은 조합이 실제로 3~4건).
    """
    for source in _SOURCE_PRIORITY:
        for row in rows:
            if row["source"] != source:
                continue
            if source in _MS_COMPARABLE:
                stored = row["ms_played"]
                if stored is None or abs(int(stored) - ms_played) > MS_TOLERANCE_MS:
                    continue
            return row
    return None


def _link_sessions(
    conn: sqlite3.Connection,
    streams: list[dict[str, Any]],
    stats: TechlogStats,
    *,
    batch_id: int,
    now: int,
    write: bool,
) -> None:
    for s in streams:
        started = s["ended_at"] - s["ms_played"]
        rows = conn.execute(
            SELECT_SESSIONS_NEAR,
            (s["track_uri"], started, MATCH_WINDOW_MS, started),
        ).fetchall()
        match = _pick_session(rows, s["ms_played"])

        if match is not None and match["source"] != "recently_played":
            # 붙이기만 한다. ms_played 를 덮어쓰지 않는다 — 폴러 값이 기준이다.
            s["session_id"] = int(match["id"])
            stats.attached[str(match["source"])] += 1
        elif s["ms_played"] <= 0:
            # 0ms 는 큐 아티팩트다. 새 세션으로 만들지 않는다 (export 와 같은 규칙).
            stats.skipped_zero_ms += 1
        else:
            stats.inserted += 1
            if write:
                cur = conn.execute(
                    INSERT_TECHLOG_SESSION,
                    (
                        s["track_uri"],
                        started,
                        s["ended_at"],
                        s["ms_played"],
                        s["device_type"],
                        s["shuffle"],
                        s["reason_end"],
                        s["reason_start"],
                        now,
                        now,
                    ),
                )
                if cur.rowcount:
                    s["session_id"] = int(cur.lastrowid or 0)
            if match is not None:
                # RP 에는 ms_played 가 없고 기술 로그에는 있다. 지우지 않고
                # 대체 표시만 한다 (export 임포터와 같은 논리).
                stats.superseded_rp += 1
                if write and s["session_id"]:
                    conn.execute(MARK_SUPERSEDED, (s["session_id"], now, int(match["id"])))

        if write:
            conn.execute(
                INSERT_TECHLOG_STREAM,
                (
                    batch_id,
                    s["ended_at"],
                    s["track_uri"],
                    s["ms_played"],
                    s["reason_start"],
                    s["reason_end"],
                    s["shuffle"],
                    s["media_type"],
                    s["is_downloaded"],
                    s["social_listening"],
                    s["displayed_track_uri"],
                    s["audio_id"],
                    s["device_type"],
                    s["device_model"],
                    s["os_name"],
                    s["os_version"],
                    s["app_version"],
                    s["conn_country"],
                    s["playback_id"],
                    s["est_total_ms"],
                    s["n_seekback"],
                    s["ms_seekback"],
                    s["n_seekfwd"],
                    s["ms_seekfwd"],
                    s["n_stutter"],
                    s["boombox_gap_ms"],
                    s["session_id"],
                ),
            )


# ---------------------------------------------------------------------------
# 컨텍스트 증거
# ---------------------------------------------------------------------------


def _event_key(kind: str, record: dict[str, Any]) -> str:
    """내용 해시. NULL 이 섞인 컬럼 조합을 UNIQUE 로 쓸 수 없어서 필요하다."""
    return sha256_hex(kind + "\x00" + json.dumps(record, sort_keys=True, ensure_ascii=False))


def _insert_event(conn: sqlite3.Connection, params: tuple[Any, ...], event_key: str) -> int | None:
    cur = conn.execute(INSERT_CONTEXT_EVENT, params)
    if cur.rowcount:
        return int(cur.lastrowid or 0)
    row = conn.execute(SELECT_CONTEXT_EVENT_ID, (event_key,)).fetchone()
    return int(row[0]) if row else None


def _import_context_events(
    conn: sqlite3.Connection,
    files: dict[str, Path],
    stats: TechlogStats,
    *,
    batch_id: int,
    write: bool,
) -> list[ContextSequence]:
    """셔플 시퀀스와 `TrackNotPlayed` 를 하나의 컨텍스트 스트림으로 정규화한다.

    `AdvanceStuck` 은 일부러 뺐다 — `source_start` 가 컨텍스트의 *종류*만
    주고 `context_uri` 는 없어서 어느 플레이리스트인지 특정할 수 없다.
    """
    sequences: list[ContextSequence] = []

    for r in _load(files["shuffle"]) if "shuffle" in files else []:
        emitted = _ts(r)
        context = r.get("message_context_uri")
        tracks = r.get("message_shuffled_sequence")
        if emitted is None or not isinstance(context, str) or not isinstance(tracks, list):
            continue
        key = _event_key("shuffle_sequence", r)
        event_id = None
        if write:
            event_id = _insert_event(
                conn,
                (
                    batch_id,
                    emitted,
                    "shuffle_sequence",
                    context,
                    None,
                    r.get("message_trigger_command"),
                    r.get("message_shuffle_algorithm_setting"),
                    r.get("message_play_context_length"),
                    None,
                    r.get("message_shuffle_sequence_uuid"),
                    key,
                ),
                key,
            )
            positions = r.get("message_shuffled_sequence_original_position") or []
            for i, uri in enumerate(tracks):
                if not isinstance(uri, str):
                    continue
                original = positions[i] if i < len(positions) else None
                conn.execute(
                    INSERT_CONTEXT_ITEM,
                    (event_id, i, uri, original if isinstance(original, int) else None),
                )
                stats.context_items += 1
        else:
            stats.context_items += sum(1 for u in tracks if isinstance(u, str))
        stats.context_events += 1
        sequences.append(
            ContextSequence(
                emitted_at=emitted,
                context_uri=context,
                tracks=frozenset(u for u in tracks if isinstance(u, str)),
                event_id=event_id,
            )
        )

    for r in _load(files["not_played"]) if "not_played" in files else []:
        emitted = _ts(r)
        context = r.get("message_play_context")
        if emitted is None:
            continue
        # `message_play_track` 은 URI 가 아니라 원시 id 라 track_uri 에 넣지
        # 않는다. 이 이벤트는 트랙이 아니라 *컨텍스트 타임라인*으로 쓴다.
        key = _event_key("track_not_played", r)
        if write:
            _insert_event(
                conn,
                (
                    batch_id,
                    emitted,
                    "track_not_played",
                    context
                    if isinstance(context, str) and context.startswith("spotify:")
                    else None,
                    r.get("message_source_start") or None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    key,
                ),
                key,
            )
        stats.context_events += 1

    return sequences


def _write_evidence(
    conn: sqlite3.Connection,
    streams: list[dict[str, Any]],
    sequences: list[ContextSequence],
    stats: TechlogStats,
    *,
    now: int,
    write: bool,
) -> None:
    index = build_index(sequences)
    for s in streams:
        found = attribute(
            s["track_uri"],
            s["ended_at"],
            index,
            shuffle=None if s["shuffle"] is None else bool(s["shuffle"]),
        )
        if found is None:
            continue
        stats.evidence[found.confidence] += 1
        if found.context_type is None:
            # 결정표가 모르는 종류다 (`spotify:search:` 등). 증거는 남기되
            # 타입을 지어내지 않는다 — 지어내면 self 판정이 unknown 으로 퇴행한다.
            stats.evidence_untyped += 1
        if not write or not s["session_id"]:
            continue
        conn.execute(
            UPSERT_SESSION_CONTEXT_EVIDENCE,
            (
                s["session_id"],
                RULES_VERSION,
                found.context_uri,
                found.context_type,
                "shuffle_sequence",
                found.confidence,
                found.event_id,
                found.lag_ms,
                now,
            ),
        )


def _write_duration_hints(
    conn: sqlite3.Connection,
    streams: list[dict[str, Any]],
    stats: TechlogStats,
    *,
    now: int,
    write: bool,
) -> None:
    """트랙별 곡 길이 힌트. 관측이 갈리면 값을 남기되 표시해 둔다.

    실제로 갈린 곡이 3개 있었다 (최대 155초 차이 — 다른 버전이거나 오결합).
    행을 버리지 않고 `disagreements` 로 표시만 하는 것은 이 리포의 규칙 4와
    같은 이유다. 소비하는 쪽이 `disagreements = 0` 으로 거른다.
    """
    observed: dict[str, list[int]] = {}
    for s in streams:
        total = s["est_total_ms"]
        if isinstance(total, int) and total > 0:
            observed.setdefault(s["track_uri"], []).append(total)

    for uri, values in observed.items():
        counts = Counter(values)
        duration, _ = counts.most_common(1)[0]
        disagreements = len(values) - counts[duration]
        stats.duration_hints += 1
        if disagreements:
            stats.duration_conflicts += 1
        if write:
            conn.execute(
                UPSERT_TRACK_DURATION_HINT,
                (uri, duration, "techlog_boombox", len(values), disagreements, now),
            )


# ---------------------------------------------------------------------------
# 알고리즘 노출
# ---------------------------------------------------------------------------


def _import_exposures(
    conn: sqlite3.Connection,
    files: dict[str, Path],
    stats: TechlogStats,
    *,
    batch_id: int,
    write: bool,
) -> None:
    """무엇을 밀어 줬나. 네 surface 를 한 모양으로 담는다."""
    for key, surface in (
        ("daylist", "daylist"),
        ("on_repeat", "on_repeat"),
        ("release_radar", "release_radar"),
        ("continuum", "continuum"),
    ):
        if key not in files:
            continue
        for r in _load(files[key]):
            observed = _ts(r)
            if observed is None:
                continue
            context, title, position, items, meta = _exposure_fields(surface, r)
            event_key = _event_key(surface, r)
            stats.exposures += 1
            stats.exposure_items += len(items)
            if not write:
                continue
            cur = conn.execute(
                INSERT_ALGO_EXPOSURE,
                (
                    batch_id,
                    observed,
                    surface,
                    context,
                    title,
                    position,
                    json.dumps(meta, ensure_ascii=False) if meta else None,
                    event_key,
                ),
            )
            if not cur.rowcount:
                continue
            exposure_id = int(cur.lastrowid or 0)
            for i, uri in enumerate(items):
                conn.execute(INSERT_ALGO_EXPOSURE_ITEM, (exposure_id, i, uri))


def _exposure_fields(
    surface: str, r: dict[str, Any]
) -> tuple[str | None, str | None, int | None, list[str], dict[str, Any] | None]:
    def uris(value: object) -> list[str]:
        return [u for u in value if isinstance(u, str)] if isinstance(value, list) else []

    if surface == "daylist":
        # daylist 에는 context_uri 가 아예 없다. seed/anti-seed 가 진짜 값어치다.
        return (
            None,
            r.get("message_playlist_title"),
            None,
            uris(r.get("message_tracks")),
            {
                "seed_uris": uris(r.get("message_seed_uris")),
                "anti_seed_uris": uris(r.get("message_anti_seed_uris")),
                "daypart": r.get("message_daypart"),
                "day_of_week": r.get("message_day_of_week"),
                "description": r.get("message_playlist_description"),
            },
        )
    if surface == "on_repeat":
        mix = r.get("message_mix_id")
        return (
            f"spotify:playlist:{mix}" if isinstance(mix, str) else None,
            None,
            None,
            uris(r.get("message_contents")),
            {"type": r.get("message_type")},
        )
    if surface == "release_radar":
        return (
            r.get("message_context_uri"),
            None,
            None,
            uris(r.get("message_track_uris")),
            {"is_made_for_user": r.get("message_is_made_for_user")},
        )
    position = r.get("message_position")
    return (
        r.get("message_content_uri"),
        r.get("message_name"),
        position if isinstance(position, int) else None,
        uris(r.get("message_items_uris")),
        None,
    )
