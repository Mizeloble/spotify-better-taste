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
    "INSERT_RAW_RESPONSE",
    "SELECT_COLLECTION_HEALTH",
    "SELECT_SHAPE",
    "SELECT_TABLE_NAMES",
    "UPDATE_SHAPE_ALERTED",
    "UPDATE_SHAPE_SAMPLE",
    "UPSERT_SCHEMA_SHAPE",
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
