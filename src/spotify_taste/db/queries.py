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
    "CONFIRM_SESSION_BY_RP",
    "COUNT_SHAPES_FOR_ENDPOINT",
    "COUNT_TABLE_ROWS",
    "DELETE_EXPIRED_RAW",
    "FINALIZE_OPEN_SESSION",
    "GET_CALIBRATION",
    "GET_CURSOR",
    "INSERT_COLLECTOR_GAP",
    "INSERT_PLAYER_SAMPLE",
    "INSERT_RAW_RESPONSE",
    "INSERT_RP_SESSION",
    "MARK_PHANTOM",
    "MARK_SUPERSEDED",
    "SELECT_CALIBRATION_PAIRS",
    "SELECT_COLLECTION_HEALTH",
    "SELECT_LAST_SAMPLE_AT",
    "SELECT_LIBRARY_URIS",
    "SELECT_ME_ID",
    "SELECT_NULL_CONTEXT_RATIO",
    "SELECT_OPEN_SESSIONS",
    "SELECT_ORIGIN_DISTRIBUTION",
    "SELECT_OWN_PLAYLIST_TRACK_URIS",
    "SELECT_PLAYLISTS_NEEDING_REFRESH",
    "SELECT_PLAYLIST_META",
    "SELECT_POLLER_MATCH",
    "SELECT_RP_NEAR",
    "SELECT_SAMPLES_SINCE",
    "SELECT_SESSIONS_FOR_CLASSIFY",
    "SELECT_SHAPE",
    "SELECT_TABLE_NAMES",
    "SELECT_TOP_UNRESOLVED_PLAYLISTS",
    "SET_CALIBRATION",
    "SET_CURSOR",
    "UPDATE_SESSION_ORIGIN",
    "UPDATE_SHAPE_ALERTED",
    "UPDATE_SHAPE_SAMPLE",
    "UPSERT_PLAYLIST_STATUS",
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

# --- recently-played 병합 --------------------------------------------------

# 폴러가 이미 이 재생을 설명하고 있는가. 가장 가까운 것 하나만 본다.
SELECT_POLLER_MATCH = """
SELECT * FROM play_session
 WHERE track_uri = ? AND source = 'poller' AND superseded_by IS NULL
   AND abs(started_at - ?) <= ?
 ORDER BY abs(started_at - ?) LIMIT 1
"""

# 같은 트랙의 RP 세션이 이미 근처에 있는가 (팬텀/근접 중복 방지).
SELECT_RP_NEAR = """
SELECT id, started_at FROM play_session
 WHERE track_uri = ? AND source = 'recently_played'
   AND abs(started_at - ?) <= ?
 LIMIT 1
"""

INSERT_RP_SESSION = """
INSERT INTO play_session
  (track_uri, started_at, ended_at, ms_played, duration_ms,
   completion_ratio, listened_ratio, inferred_completion_ratio,
   context_uri, context_type, source, timing_confidence, end_reason,
   is_open, created_at, updated_at)
VALUES (?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, 'recently_played', 'low',
        'rp_inferred', 0, ?, ?)
ON CONFLICT (track_uri, started_at) DO NOTHING
"""

# RP 는 확인과 context 보충만 기여한다. 타이밍·실측은 폴러가 항상 이긴다.
CONFIRM_SESSION_BY_RP = """
UPDATE play_session
   SET confirmed_by_rp = 1,
       context_uri  = COALESCE(context_uri, ?),
       context_type = COALESCE(context_type, ?),
       updated_at   = ?
 WHERE id = ?
"""

MARK_SUPERSEDED = """
UPDATE play_session SET superseded_by = ?, updated_at = ? WHERE id = ?
"""

MARK_PHANTOM = """
UPDATE play_session SET suspect_phantom = 1, updated_at = ? WHERE id = ?
"""

# 보정용: 폴러와 RP 가 겹친 구간의 시각 차이 표본.
SELECT_CALIBRATION_PAIRS = """
SELECT rp.started_at - p.started_at AS delta
  FROM play_session rp
  JOIN play_session p
    ON p.track_uri = rp.track_uri
   AND p.source = 'poller'
   AND abs(p.started_at - rp.started_at) <= ?
 WHERE rp.source = 'recently_played'
"""

# --- 분류 -----------------------------------------------------------------

SELECT_ME_ID = "SELECT id FROM me ORDER BY fetched_at DESC LIMIT 1"

# 분류 후보. `silence_before_ms` 는 직전 재생이 끝난 뒤 얼마나 조용했는지 —
# null-context 를 '사람이 직접 튼 것'으로 승격할 때 쓰는 증거다.
# 창 함수가 전체 순서를 봐야 정확하므로 여기서 거르지 않고 호출부가 고른다.
SELECT_SESSIONS_FOR_CLASSIFY = """
SELECT id, track_uri, started_at, context_uri, context_type, shuffle,
       context_origin AS previous_origin,
       context_origin_rules_version AS rules_version,
       started_at - LAG(ended_at) OVER (ORDER BY started_at, id) AS silence_before_ms
  FROM play_session
 WHERE superseded_by IS NULL AND suspect_phantom = 0
 ORDER BY started_at, id
"""

UPDATE_SESSION_ORIGIN = """
UPDATE play_session
   SET context_origin = ?, context_origin_reason = ?,
       context_origin_rules_version = ?, updated_at = ?
 WHERE id = ?
"""

SELECT_PLAYLIST_META = """
SELECT uri, name, owner_id, is_generated, fetch_status, refresh_after
  FROM playlist_meta WHERE uri = ?
"""

# 세션이 참조하지만 캐시에 없거나 TTL 이 지난 플레이리스트.
# 실패도 캐시하므로(refresh_after 로), 삭제된 플레이리스트를 매번 다시
# 조회하는 일이 없다.
SELECT_PLAYLISTS_NEEDING_REFRESH = """
SELECT DISTINCT s.context_uri AS uri
  FROM play_session s
  LEFT JOIN playlist_meta m ON m.uri = s.context_uri
 WHERE s.context_type = 'playlist' AND s.context_uri IS NOT NULL
   AND (m.uri IS NULL OR (m.fetch_status != 'ok' AND m.refresh_after < ?))
 LIMIT ?
"""

UPSERT_PLAYLIST_STATUS = """
INSERT INTO playlist_meta
  (uri, id, name, owner_id, owner_display_name, owner_uri, snapshot_id,
   total_items, fetched_at, fetch_status, http_status, refresh_after)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (uri) DO UPDATE SET
  name = COALESCE(excluded.name, playlist_meta.name),
  owner_id = COALESCE(excluded.owner_id, playlist_meta.owner_id),
  owner_display_name = COALESCE(excluded.owner_display_name,
                                playlist_meta.owner_display_name),
  snapshot_id = COALESCE(excluded.snapshot_id, playlist_meta.snapshot_id),
  fetched_at = excluded.fetched_at,
  fetch_status = excluded.fetch_status,
  http_status = excluded.http_status,
  refresh_after = excluded.refresh_after
"""

SELECT_LIBRARY_URIS = "SELECT track_uri FROM library_track WHERE removed_at IS NULL"

# 내 플레이리스트에 수록된 트랙. null-context 승격의 두 번째 증거.
SELECT_OWN_PLAYLIST_TRACK_URIS = """
SELECT DISTINCT i.track_uri
  FROM playlist_item i
  JOIN playlist_meta m ON m.uri = i.playlist_uri
 WHERE m.owner_id = ? AND m.is_generated = 0
"""

SELECT_ORIGIN_DISTRIBUTION = """
SELECT COALESCE(context_origin, '(미분류)') AS origin, count(*) AS n
  FROM v_clean_session GROUP BY 1 ORDER BY n DESC
"""

SELECT_NULL_CONTEXT_RATIO = """
SELECT
  count(*) AS total,
  sum(CASE WHEN context_uri IS NULL THEN 1 ELSE 0 END) AS null_context
FROM v_clean_session
"""

SELECT_TOP_UNRESOLVED_PLAYLISTS = """
SELECT context_uri, count(*) AS n
  FROM v_clean_session
 WHERE context_origin = 'unknown' AND context_type = 'playlist'
 GROUP BY context_uri ORDER BY n DESC LIMIT ?
"""

# --- 메타데이터 백필 ------------------------------------------------------

# 세션에서 관측된 트랙 중 아직 메타를 모르는 것을 큐에 올린다.
# `WHERE true` 는 장식이 아니다 — INSERT…SELECT 뒤의 ON CONFLICT 는 SQLite
# 파서에게 모호해서 WHERE 절이 없으면 문법 오류가 난다.
ENQUEUE_TRACKS_FROM_SESSIONS = """
INSERT INTO track (uri, fetch_status)
SELECT DISTINCT track_uri, 'pending' FROM play_session WHERE true
ON CONFLICT (uri) DO NOTHING
"""

# **재생 횟수 내림차순**. 실제로 듣는 트랙이 첫날 밤에 해결되고 롱테일은
# 천천히 빠진다. 콜드 스타트가 수만 콜이라 순서가 곧 체감 속도다.
SELECT_PENDING_TRACKS = """
SELECT t.uri, COALESCE(c.n, 0) AS play_count
  FROM track t
  LEFT JOIN (SELECT track_uri, count(*) AS n FROM play_session GROUP BY track_uri) c
    ON c.track_uri = t.uri
 WHERE t.fetch_status IN ('pending', 'error')
 ORDER BY play_count DESC, t.uri
 LIMIT ?
"""

SELECT_PENDING_ARTISTS = """
SELECT a.uri, COALESCE(c.n, 0) AS play_count
  FROM artist a
  LEFT JOIN (
        SELECT ta.artist_uri, count(*) AS n
          FROM track_artist ta JOIN play_session s ON s.track_uri = ta.track_uri
         GROUP BY ta.artist_uri
       ) c ON c.artist_uri = a.uri
 WHERE a.fetch_status IN ('pending', 'error')
 ORDER BY play_count DESC, a.uri
 LIMIT ?
"""

SELECT_PENDING_ALBUMS = """
SELECT uri, 0 AS play_count FROM album
 WHERE fetch_status IN ('pending', 'error') ORDER BY uri LIMIT ?
"""

UPDATE_TRACK_META = """
UPDATE track SET id = ?, name = ?, album_uri = ?, duration_ms = ?, explicit = ?,
       isrc = ?, disc_number = ?, track_number = ?, is_local = ?,
       fetched_at = ?, fetch_status = 'ok', http_status = ?,
       fetch_attempts = fetch_attempts + 1, raw_id = ?
 WHERE uri = ?
"""

UPDATE_ARTIST_META = """
UPDATE artist SET id = ?, name = ?, genres_json = ?, image_url = ?,
       fetched_at = ?, fetch_status = 'ok', http_status = ?,
       fetch_attempts = fetch_attempts + 1, raw_id = ?
 WHERE uri = ?
"""

UPDATE_ALBUM_META = """
UPDATE album SET id = ?, name = ?, album_type = ?, release_date = ?,
       release_date_precision = ?, label = ?, total_tracks = ?, genres_json = ?,
       fetched_at = ?, fetch_status = 'ok', http_status = ?,
       fetch_attempts = fetch_attempts + 1, raw_id = ?
 WHERE uri = ?
"""

# 404 는 'not_found' 로 못 박아 다시 시도하지 않는다. 없는 트랙을 매일 밤
# 다시 물어보는 것은 예산 낭비다.
MARK_FETCH_FAILED = """
UPDATE {table} SET fetch_status = ?, http_status = ?, fetched_at = ?,
       fetch_attempts = fetch_attempts + 1
 WHERE uri = ?
"""

ENSURE_ARTIST = (
    "INSERT INTO artist (uri, fetch_status) VALUES (?, 'pending') ON CONFLICT DO NOTHING"
)
ENSURE_ALBUM = "INSERT INTO album (uri, fetch_status) VALUES (?, 'pending') ON CONFLICT DO NOTHING"
LINK_TRACK_ARTIST = """
INSERT INTO track_artist (track_uri, artist_uri, position) VALUES (?, ?, ?)
ON CONFLICT (track_uri, position) DO UPDATE SET artist_uri = excluded.artist_uri
"""

SELECT_BACKFILL_STATUS = """
SELECT
  (SELECT count(*) FROM track  WHERE fetch_status IN ('pending','error')) AS tracks_pending,
  (SELECT count(*) FROM track  WHERE fetch_status = 'ok')                 AS tracks_ok,
  (SELECT count(*) FROM artist WHERE fetch_status IN ('pending','error')) AS artists_pending,
  (SELECT count(*) FROM artist WHERE fetch_status = 'ok')                 AS artists_ok,
  (SELECT count(*) FROM album  WHERE fetch_status IN ('pending','error')) AS albums_pending,
  (SELECT count(*) FROM album  WHERE fetch_status = 'ok')                 AS albums_ok,
  (SELECT count(*) FROM track  WHERE fetch_status = 'not_found')          AS tracks_missing
"""

# --- 커서 / 보정 상수 -----------------------------------------------------

GET_CURSOR = "SELECT value FROM sync_cursor WHERE name = ?"

SET_CURSOR = """
INSERT INTO sync_cursor (name, value, updated_at) VALUES (?, ?, ?)
ON CONFLICT (name) DO UPDATE SET value = excluded.value,
                                 updated_at = excluded.updated_at
"""

GET_CALIBRATION = "SELECT value, sample_size FROM calibration WHERE key = ?"

SET_CALIBRATION = """
INSERT INTO calibration (key, value, sample_size, updated_at) VALUES (?, ?, ?, ?)
ON CONFLICT (key) DO UPDATE SET value = excluded.value,
                                sample_size = excluded.sample_size,
                                updated_at = excluded.updated_at
"""

FINALIZE_OPEN_SESSION = """
UPDATE play_session
   SET is_open = 0, end_reason = ?, updated_at = ?
 WHERE id = ?
"""
