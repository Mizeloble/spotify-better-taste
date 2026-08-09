"""무엇/왜
프로젝트의 모든 SQL 이 사는 곳. 다른 모듈은 여기 상수를 가져다 쓰기만 한다.

설계 원칙
- 왜 한 곳에 모으나: "이 컬럼을 누가 쓰지"에 grep 한 번으로 답할 수 있어야
  스키마를 안전하게 고칠 수 있다. 인라인 SQL 은 그 질문에 답하지 못한다.
- 이름은 `<동사>_<대상>` 꼴. 상수라서 대문자.
- 분석·학습 쿼리는 `play_session` 이 아니라 **`v_clean_session`** 을 읽는다.
  대체·팬텀 제외 조건을 호출부가 잊을 수 없게 하려고 뷰를 둔 것이다.

어떻게 확인
    uv run pytest tests/test_db.py
"""

from __future__ import annotations

__all__ = [
    "COUNT_SHAPES_FOR_ENDPOINT",
    "COUNT_TABLE_ROWS",
    "DELETE_EXPIRED_RAW",
    "FINALIZE_OPEN_SESSION",
    "INSERT_COLLECTOR_GAP",
    "INSERT_PLAYER_SAMPLE",
    "INSERT_RAW_RESPONSE",
    "SELECT_COLLECTION_HEALTH",
    "SELECT_LAST_SAMPLE_AT",
    "SELECT_OPEN_SESSIONS",
    "SELECT_SAMPLES_SINCE",
    "SELECT_SHAPE",
    "SELECT_TABLE_NAMES",
    "UPDATE_SHAPE_ALERTED",
    "UPDATE_SHAPE_SAMPLE",
    "UPSERT_SCHEMA_SHAPE",
    "UPSERT_SESSION",
]

# `db stats` 가 순회할 대상. sqlite 내부 테이블과 뷰는 제외한다.
SELECT_TABLE_NAMES = """
SELECT name FROM sqlite_schema
 WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
 ORDER BY name
"""

# 테이블명은 바인딩 파라미터로 넣을 수 없다. 호출부가 SELECT_TABLE_NAMES 로
# 얻은 이름만 여기에 포맷하므로 외부 입력이 섞이지 않는다.
COUNT_TABLE_ROWS = "SELECT count(*) AS n FROM {table}"

# 수집이 실제로 살아 있는지 한눈에. heartbeat 파일이 아니라 데이터를 본다.
SELECT_COLLECTION_HEALTH = """
SELECT
  (SELECT max(sampled_at) FROM player_sample)                      AS last_sample_at,
  (SELECT count(*) FROM player_sample)                             AS sample_count,
  (SELECT count(*) FROM v_clean_session)                           AS session_count,
  (SELECT count(*) FROM v_clean_session WHERE is_open = 1)          AS open_session_count,
  (SELECT count(*) FROM v_clean_session WHERE source = 'poller')    AS poller_session_count,
  (SELECT count(*) FROM v_clean_session
    WHERE source = 'recently_played')                              AS rp_session_count,
  (SELECT count(*) FROM play_session WHERE superseded_by IS NOT NULL) AS superseded_count,
  (SELECT count(*) FROM play_session WHERE suspect_phantom = 1)    AS phantom_count,
  (SELECT count(*) FROM collector_gap WHERE reconciled_at IS NULL) AS open_gap_count
"""

# --- 원본 아카이브 --------------------------------------------------------

INSERT_RAW_RESPONSE = """
INSERT INTO raw_api_response
  (fetched_at, endpoint, request_params, http_status,
   body, body_sha256, shape_hash, source_job, keep_until)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# keep_until 이 NULL 인 행은 영구 보관 — 절대 지우지 않는다.
DELETE_EXPIRED_RAW = """
DELETE FROM raw_api_response
 WHERE keep_until IS NOT NULL AND keep_until < ?
"""

COUNT_SHAPES_FOR_ENDPOINT = """
SELECT count(*) FROM api_schema_shape WHERE endpoint = ?
"""

SELECT_SHAPE = """
SELECT shape_hash, first_seen_at, alerted
  FROM api_schema_shape
 WHERE endpoint = ? AND shape_hash = ?
"""

UPSERT_SCHEMA_SHAPE = """
INSERT INTO api_schema_shape
  (endpoint, shape_hash, key_paths, first_seen_at, last_seen_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT (endpoint, shape_hash) DO UPDATE SET
  last_seen_at = excluded.last_seen_at
"""

# 모양마다 예시 원문을 하나씩 붙여 둔다 (알림 여부와 무관).
UPDATE_SHAPE_SAMPLE = """
UPDATE api_schema_shape
   SET sample_raw_id = ?
 WHERE endpoint = ? AND shape_hash = ?
"""

# alerted 는 '실제로 알림을 보냈다'만 뜻한다. 엔드포인트를 처음 본 경우는
# 변화가 아니므로 알리지 않고, 따라서 여기 걸리지 않는다.
UPDATE_SHAPE_ALERTED = """
UPDATE api_schema_shape
   SET alerted = 1
 WHERE endpoint = ? AND shape_hash = ?
"""

# --- 수집 -----------------------------------------------------------------

INSERT_PLAYER_SAMPLE = """
INSERT INTO player_sample
  (sampled_at, http_status, is_playing, track_uri, duration_ms, progress_ms,
   context_uri, context_type, device_id, device_type, device_is_private,
   shuffle_state, repeat_state, currently_playing_type, spotify_timestamp, raw_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SELECT_SAMPLES_SINCE = """
SELECT * FROM player_sample
 WHERE sampled_at >= ? AND sampled_at <= ?
 ORDER BY sampled_at, id
"""

SELECT_LAST_SAMPLE_AT = "SELECT max(sampled_at) FROM player_sample"

INSERT_COLLECTOR_GAP = """
INSERT INTO collector_gap (started_at, ended_at, reason) VALUES (?, ?, ?)
"""

# --- play_session ---------------------------------------------------------

# 열린 세션은 매 틱 이 쿼리로 저장된다. ended_at 에는 마지막 샘플 시각이
# 들어가 있어 재시작 때 '얼마나 오래된 반열림인가'를 바로 알 수 있다.
UPSERT_SESSION = """
INSERT INTO play_session
  (track_uri, started_at, ended_at, ms_played, duration_ms, max_progress_ms,
   completion_ratio, listened_ratio, context_uri, context_type,
   device_type, device_id, device_is_private, shuffle,
   source, timing_confidence, end_reason, is_open,
   sample_count, seek_forward_count, first_sample_id, last_sample_id,
   created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (track_uri, started_at) DO UPDATE SET
  ended_at           = excluded.ended_at,
  ms_played          = excluded.ms_played,
  duration_ms        = excluded.duration_ms,
  max_progress_ms    = excluded.max_progress_ms,
  completion_ratio   = excluded.completion_ratio,
  listened_ratio     = excluded.listened_ratio,
  -- context 는 뒤늦게 채워질 수 있으므로 한번 값이 생기면 덮지 않는다.
  context_uri        = COALESCE(play_session.context_uri, excluded.context_uri),
  context_type       = COALESCE(play_session.context_type, excluded.context_type),
  end_reason         = excluded.end_reason,
  is_open            = excluded.is_open,
  sample_count       = excluded.sample_count,
  seek_forward_count = excluded.seek_forward_count,
  last_sample_id     = excluded.last_sample_id,
  updated_at         = excluded.updated_at
"""

SELECT_OPEN_SESSIONS = """
SELECT * FROM play_session WHERE is_open = 1 ORDER BY started_at
"""

FINALIZE_OPEN_SESSION = """
UPDATE play_session
   SET is_open = 0, end_reason = ?, updated_at = ?
 WHERE id = ?
"""
