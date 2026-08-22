"""기술 로그 임포터.

지키는 불변식: **사실 행을 늘리지 않는다**(이미 있는 재생에는 붙이기만),
**매칭 조건이 출처마다 다르다**(폴러는 독립 측정이라 ms 를 비교하면 안 된다),
**멱등하다**(NULL 이 섞인 입력을 포함해), **유추를 관측과 섞지 않는다**.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spotify_taste._common import iso_to_epoch_ms
from spotify_taste.classify.patterns import RULES_VERSION
from spotify_taste.classify.recompute import recompute
from spotify_taste.db.connection import tx
from spotify_taste.importer.techlog import discover_files, import_techlog

A = "spotify:track:AAAA"
B = "spotify:track:BBBB"
PLAYLIST = "spotify:playlist:37i9dQZF1F5p3rmiWPIYgZ"
TS = "2026-03-01T12:00:00.500Z"
ENDED = iso_to_epoch_ms(TS)
MS = 180_000
STARTED = ENDED - MS


def _at(offset_ms: int) -> str:
    """ENDED 기준 상대 시각을 ISO 로."""
    from datetime import UTC, datetime

    dt = datetime.fromtimestamp((ENDED + offset_ms) / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _stream(**over) -> dict:
    base = {
        "context_device_type": "mobile",
        "context_device_model": "SM-S931N",
        "context_os_name": "android",
        "context_os_version": "16",
        "context_application_version": "9.1.60",
        "context_conn_country": "KR",
        "message_media_type": "audio",
        "message_reason_start": "trackdone",
        "message_reason_end": "trackdone",
        "message_ms_played": MS,
        "message_shuffle": True,
        "message_content_uri": A,
        "message_displayed_content_uri": "",
        "message_content_is_downloaded": False,
        "message_social_listening": False,
        "message_audio_id": "audio-1",
        "timestamp_utc": TS,
    }
    base.update(over)
    return base


def _boombox(**over) -> dict:
    base = {
        "message_playback_id": "pb-1",
        "message_ms_total_est": 200_000,
        "message_n_stutter": 0,
        "message_n_seekback": 1,
        "message_ms_seekback": 100,
        "message_n_seekfwd": 0,
        "message_ms_seekfwd": 0,
        "timestamp_utc": TS,
    }
    base.update(over)
    return base


def _shuffle(**over) -> dict:
    base = {
        "message_shuffled_sequence": [A, B],
        "message_shuffled_sequence_original_position": [1, 2],
        "message_context_uri": PLAYLIST,
        "message_trigger_command": "play",
        "message_shuffle_algorithm_setting": "FRESH",
        "message_play_context_length": 2,
        "message_shuffle_sequence_uuid": "uuid-1",
        "timestamp_utc": _at(-60_000),
    }
    base.update(over)
    return base


def _not_played(**over) -> dict:
    # sequence_uuid 가 없다 — UNIQUE 에 NULL 을 넣으면 재임포트가 행을 두 배로
    # 만드는 그 입력이다.
    base = {
        "message_play_context": PLAYLIST,
        "message_source_start": "playlist",
        "message_reason_not_played": "not-unpaused",
        "message_play_track": "3608df95b4924044ab4ec7e21df82346",
        "timestamp_utc": _at(-30_000),
    }
    base.update(over)
    return base


def _daylist(**over) -> dict:
    # context_uri 필드가 아예 없는 입력.
    base = {
        "message_playlist_title": "indie soul sunday morning",
        "message_tracks": [A, B],
        "message_seed_uris": [A],
        "message_anti_seed_uris": [],
        "message_daypart": "morning",
        "message_day_of_week": "sunday",
        "message_playlist_description": "…",
        "timestamp_utc": _at(-3_600_000),
    }
    base.update(over)
    return base


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    d = tmp_path / "techlog"
    d.mkdir()
    return d


def _write(directory: Path, name: str, records: list[dict]) -> None:
    (directory / name).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def _session(
    db: sqlite3.Connection,
    *,
    source: str,
    started_at: int = STARTED,
    ms_played: int | None = MS,
    track_uri: str = A,
) -> int:
    cur = db.execute(
        """
        INSERT INTO play_session
          (track_uri, started_at, ended_at, ms_played, source, timing_confidence,
           created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'high', 0, 0)
        """,
        (track_uri, started_at, started_at + (ms_played or 0), ms_played, source),
    )
    return int(cur.lastrowid or 0)


def _counts(db: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "play_session",
        "techlog_stream",
        "techlog_context_event",
        "techlog_context_item",
        "session_context_evidence",
        "algo_exposure",
        "algo_exposure_item",
        "track_duration_hint",
        "import_batch",
    ]
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def _run(db: sqlite3.Connection, directory: Path, *, write: bool = True):
    if not write:
        return import_techlog(db, directory, write=False)
    with tx(db):
        return import_techlog(db, directory, write=True)


# ---------------------------------------------------------------------------
# 매칭 — 여기가 틀리면 재생이 두 번 세어진다
# ---------------------------------------------------------------------------


def test_attaches_to_export_row_without_inserting(db: sqlite3.Connection, export_dir: Path) -> None:
    session_id = _session(db, source="history_export")
    _write(export_dir, "RawCoreStream.json", [_stream()])

    stats = _run(db, export_dir)

    assert stats.attached["history_export"] == 1
    assert stats.inserted == 0
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1
    linked = db.execute("SELECT session_id FROM techlog_stream").fetchone()[0]
    assert linked == session_id


def test_ms_condition_disambiguates_repeat_plays(db: sqlite3.Connection, export_dir: Path) -> None:
    """같은 곡을 반복 재생한 구간에서 시각만으로 붙이면 엉뚱한 짝이 생긴다.

    실제 데이터에서 `ms_played` 0 ↔ 156,720 같은 조합이 3~4건 나왔다.
    """
    wrong = _session(db, source="history_export", started_at=STARTED, ms_played=1_000)
    right = _session(db, source="history_export", started_at=STARTED + 800, ms_played=MS)
    _write(export_dir, "RawCoreStream.json", [_stream()])

    _run(db, export_dir)

    linked = db.execute("SELECT session_id FROM techlog_stream").fetchone()[0]
    assert linked == right, "시각이 더 가까운 wrong 이 아니라 ms 가 맞는 쪽에 붙어야 한다"
    assert linked != wrong


def test_poller_row_attaches_despite_ms_difference(
    db: sqlite3.Connection, export_dir: Path
) -> None:
    """폴러는 독립적으로 잰다 — 실측에서 최대 18초까지 벌어졌다.

    여기에 ms 조건을 걸면 이미 있는 재생이 새 세션으로 또 들어간다 (37건).
    """
    # 폴러의 started_at 은 25초 폴링에서 역산한 값이라 중앙값 12.6초 벌어진다.
    session_id = _session(db, source="poller", started_at=STARTED + 12_600, ms_played=MS - 18_000)
    _write(export_dir, "RawCoreStream.json", [_stream()])

    stats = _run(db, export_dir)

    assert stats.attached["poller"] == 1
    assert stats.inserted == 0
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1
    assert db.execute("SELECT session_id FROM techlog_stream").fetchone()[0] == session_id


def test_poller_wins_over_export(db: sqlite3.Connection, export_dir: Path) -> None:
    poller = _session(db, source="poller", started_at=STARTED + 30_000, ms_played=MS - 5_000)
    _session(db, source="history_export", started_at=STARTED, ms_played=MS)
    _write(export_dir, "RawCoreStream.json", [_stream()])

    _run(db, export_dir)

    assert db.execute("SELECT session_id FROM techlog_stream").fetchone()[0] == poller


def test_rp_row_is_superseded_not_attached(db: sqlite3.Connection, export_dir: Path) -> None:
    """RP 에는 ms_played 가 없고 기술 로그에는 있다. 대체한다 — 지우지 않고."""
    # RP 의 played_at 은 30초 반올림이라 중앙값 57초까지 벌어진다.
    rp = _session(db, source="recently_played", started_at=STARTED + 57_000, ms_played=None)
    _write(export_dir, "RawCoreStream.json", [_stream()])

    stats = _run(db, export_dir)

    assert stats.inserted == 1
    assert stats.superseded_rp == 1
    new_id = db.execute("SELECT id FROM play_session WHERE source = 'techlog'").fetchone()[0]
    assert (
        db.execute("SELECT superseded_by FROM play_session WHERE id = ?", (rp,)).fetchone()[0]
        == new_id
    )
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 2


def test_far_export_row_is_not_a_match(db: sqlite3.Connection, export_dir: Path) -> None:
    """90초 밖은 다른 재생이다. ms 가 같아도 붙이지 않는다."""
    _session(db, source="history_export", started_at=STARTED + 120_000, ms_played=MS)
    _write(export_dir, "RawCoreStream.json", [_stream()])

    stats = _run(db, export_dir)

    assert stats.inserted == 1
    assert not stats.attached


def test_unmatched_play_is_inserted(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, "RawCoreStream.json", [_stream()])

    stats = _run(db, export_dir)

    assert stats.inserted == 1
    row = db.execute(
        "SELECT source, timing_confidence, ms_played, reason_start, end_reason, context_uri"
        "  FROM play_session"
    ).fetchone()
    assert row["source"] == "techlog"
    assert row["timing_confidence"] == "high"
    assert row["ms_played"] == MS
    assert row["reason_start"] == "trackdone"
    assert row["end_reason"] == "trackdone"
    # 유추한 컨텍스트는 여기 오지 않는다 — 이 컬럼은 관측값 자리다.
    assert row["context_uri"] is None


def test_zero_ms_play_is_not_inserted(db: sqlite3.Connection, export_dir: Path) -> None:
    """0ms 는 큐 아티팩트다. 원본 로그에는 남기되 세션은 만들지 않는다."""
    _write(export_dir, "RawCoreStream.json", [_stream(message_ms_played=0)])

    stats = _run(db, export_dir)

    assert stats.inserted == 0
    assert stats.skipped_zero_ms == 1
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM techlog_stream").fetchone()[0] == 1


def test_non_audio_rows_are_skipped(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(
        export_dir,
        "RawCoreStream.json",
        [
            _stream(message_content_uri="spotify:local::::"),
            _stream(message_media_type="video"),
            _stream(message_content_uri="spotify:episode:EEEE"),
            _stream(),
        ],
    )

    stats = _run(db, export_dir)

    assert stats.stream_rows == 1
    assert stats.skipped_not_audio == 3


# ---------------------------------------------------------------------------
# 멱등성
# ---------------------------------------------------------------------------


def test_import_is_idempotent(db: sqlite3.Connection, export_dir: Path) -> None:
    """NULL 이 섞인 입력을 포함해 두 번 돌려도 행이 늘지 않는다.

    `TrackNotPlayed` 는 sequence_uuid 가, `DaylistGenerated` 는 context_uri 가
    없다. 그 컬럼들을 UNIQUE 로 걸면 SQLite 가 NULL 을 서로 다른 값으로 봐서
    재임포트가 행을 두 배로 만든다.
    """
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "BoomboxPlaybackSession.json", [_boombox()])
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle()])
    _write(export_dir, "TrackNotPlayed.json", [_not_played()])
    _write(export_dir, "DaylistGenerated.json", [_daylist()])

    _run(db, export_dir)
    first = _counts(db)
    _run(db, export_dir)
    second = _counts(db)

    second.pop("import_batch")
    expected = dict(first)
    expected.pop("import_batch")
    assert second == expected


# ---------------------------------------------------------------------------
# 곡 길이
# ---------------------------------------------------------------------------


def test_boombox_join_fills_duration(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "BoomboxPlaybackSession.json", [_boombox()])

    stats = _run(db, export_dir)

    assert stats.boombox_joined == 1
    row = db.execute(
        "SELECT est_total_ms, n_seekback, boombox_gap_ms FROM techlog_stream"
    ).fetchone()
    assert row["est_total_ms"] == 200_000
    assert row["n_seekback"] == 1
    assert row["boombox_gap_ms"] == 0
    hint = db.execute("SELECT * FROM track_duration_hint").fetchone()
    assert hint["duration_ms"] == 200_000
    assert hint["source"] == "techlog_boombox"
    assert hint["disagreements"] == 0


def test_boombox_gap_beyond_tolerance_is_ignored(db: sqlite3.Connection, export_dir: Path) -> None:
    """실측 시차는 p95 가 112ms 다. 5초씩 열면 288건에서 오결합이 난다."""
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "BoomboxPlaybackSession.json", [_boombox(timestamp_utc=_at(3_000))])

    stats = _run(db, export_dir)

    assert stats.boombox_joined == 0
    assert db.execute("SELECT est_total_ms FROM techlog_stream").fetchone()[0] is None
    assert db.execute("SELECT count(*) FROM track_duration_hint").fetchone()[0] == 0


def test_boombox_overrun_is_rejected(db: sqlite3.Connection, export_dir: Path) -> None:
    """들은 시간이 곡 길이보다 길면 다른 재생에 붙은 것이다 (5초 창에서 8건)."""
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "BoomboxPlaybackSession.json", [_boombox(message_ms_total_est=10_000)])

    stats = _run(db, export_dir)

    assert stats.boombox_rejected == 1
    assert stats.boombox_joined == 0


def test_duration_conflict_is_flagged_not_dropped(db: sqlite3.Connection, export_dir: Path) -> None:
    """관측이 갈리는 곡이 실재한다 (3곡, 최대 155초 차이). 표시만 하고 남긴다."""
    _write(
        export_dir,
        "RawCoreStream.json",
        [_stream(), _stream(timestamp_utc=_at(600_000)), _stream(timestamp_utc=_at(1_200_000))],
    )
    _write(
        export_dir,
        "BoomboxPlaybackSession.json",
        [
            _boombox(),
            _boombox(timestamp_utc=_at(600_000)),
            _boombox(timestamp_utc=_at(1_200_000), message_ms_total_est=355_000),
        ],
    )

    stats = _run(db, export_dir)

    assert stats.duration_hints == 1
    assert stats.duration_conflicts == 1
    hint = db.execute("SELECT * FROM track_duration_hint").fetchone()
    assert hint["duration_ms"] == 200_000, "다수결이 이긴다"
    assert hint["observations"] == 3
    assert hint["disagreements"] == 1


# ---------------------------------------------------------------------------
# 컨텍스트 귀속
# ---------------------------------------------------------------------------


def test_evidence_is_written_with_normalized_type(db: sqlite3.Connection, export_dir: Path) -> None:
    session_id = _session(db, source="history_export")
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle()])

    _run(db, export_dir)

    row = db.execute("SELECT * FROM session_context_evidence").fetchone()
    assert row["session_id"] == session_id
    assert row["context_uri"] == PLAYLIST
    assert row["context_type"] == "playlist"
    assert row["method"] == "shuffle_sequence"
    assert row["confidence"] == "high"
    assert row["lag_ms"] == 60_000
    assert row["rules_version"] == RULES_VERSION
    # 관측값 자리는 비어 있어야 한다.
    assert db.execute("SELECT context_uri FROM play_session").fetchone()[0] is None


def test_untyped_context_is_kept_but_not_typed(db: sqlite3.Connection, export_dir: Path) -> None:
    _session(db, source="history_export")
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(
        export_dir, "ShuffleSequenceEvent.json", [_shuffle(message_context_uri="spotify:search:x")]
    )

    stats = _run(db, export_dir)

    assert stats.evidence_untyped == 1
    row = db.execute("SELECT context_uri, context_type FROM session_context_evidence").fetchone()
    assert row["context_uri"] == "spotify:search:x"
    assert row["context_type"] is None


def test_evidence_flips_classification_to_algorithmic(
    db: sqlite3.Connection, export_dir: Path
) -> None:
    """귀속의 목적. 이게 안 되면 임포트가 아무 판정도 못 바꾼다."""
    _session(db, source="history_export")
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle()])
    _run(db, export_dir)

    with tx(db):
        recompute(db, stale_only=False, write=True)

    row = db.execute("SELECT context_origin, context_origin_reason FROM play_session").fetchone()
    assert row["context_origin"] == "algorithmic"
    # 유추로 내린 판정임이 reason 에 남아야 한다.
    assert row["context_origin_reason"] == "inferred:spotify_generated_uri_prefix"


def test_inferred_context_does_not_downgrade_direct_evidence(
    db: sqlite3.Connection, export_dir: Path
) -> None:
    """해석 못 하는 플레이리스트로 귀속돼도 라이브러리 보유 판정을 지우지 않는다.

    정규화·하향 금지 없이 돌렸을 때 self 349건이 unknown 으로 퇴행했다.
    """
    _session(db, source="history_export")
    db.execute("INSERT INTO library_track (track_uri, observed_at) VALUES (?, 0)", (A,))
    other = "spotify:playlist:0000000000000000000000"  # 37i9dQZ 접두사가 없다
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle(message_context_uri=other)])
    _run(db, export_dir)

    with tx(db):
        recompute(db, stale_only=False, write=True)

    row = db.execute("SELECT context_origin, context_origin_reason FROM play_session").fetchone()
    assert row["context_origin"] == "self"
    assert row["context_origin_reason"] == "null_ctx_library_hit"


# ---------------------------------------------------------------------------
# 노출 · 안전장치
# ---------------------------------------------------------------------------


def test_exposures_are_imported(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "DaylistGenerated.json", [_daylist()])

    stats = _run(db, export_dir)

    assert stats.exposures == 1
    assert stats.exposure_items == 2
    row = db.execute("SELECT * FROM algo_exposure").fetchone()
    assert row["surface"] == "daylist"
    assert row["context_uri"] is None
    meta = json.loads(row["meta_json"])
    assert meta["seed_uris"] == [A]
    assert meta["daypart"] == "morning"
    items = [r[0] for r in db.execute("SELECT item_uri FROM algo_exposure_item ORDER BY position")]
    assert items == [A, B]


def test_whitelist_ignores_other_files(db: sqlite3.Connection, export_dir: Path) -> None:
    """같은 export 안에 평문 IP·이메일이 있다. 목록에 없는 파일은 열지 않는다."""
    _write(export_dir, "RawCoreStream.json", [_stream()])
    (export_dir / "UserAuthenticationSuccess.json").write_text(
        # RFC 5737 문서용 주소. 실제 export 값을 픽스처에 넣지 않는다.
        json.dumps([{"message_ip_address": "192.0.2.1"}]),
        encoding="utf-8",
    )

    found = discover_files(export_dir)

    assert set(found) == {"stream"}


def test_dry_run_writes_nothing(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, "RawCoreStream.json", [_stream()])
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle()])
    _write(export_dir, "BoomboxPlaybackSession.json", [_boombox()])

    stats = _run(db, export_dir, write=False)

    assert stats.inserted == 1
    assert stats.stream_rows == 1
    assert sum(stats.evidence.values()) == 1
    assert all(n == 0 for n in _counts(db).values())


def test_missing_stream_file_is_a_no_op(db: sqlite3.Connection, export_dir: Path) -> None:
    _write(export_dir, "ShuffleSequenceEvent.json", [_shuffle()])

    stats = _run(db, export_dir, write=False)

    assert stats.stream_rows == 0
    assert all(n == 0 for n in _counts(db).values())
