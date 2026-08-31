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
    "INSERT_ALGO_EXPOSURE",
    "INSERT_ALGO_EXPOSURE_ITEM",
    "INSERT_COLLECTOR_GAP",
    "INSERT_CONTEXT_EVENT",
    "INSERT_CONTEXT_ITEM",
    "INSERT_IMPORT_BATCH",
    "INSERT_PLAYER_SAMPLE",
    "INSERT_RAW_RESPONSE",
    "INSERT_RP_SESSION",
    "INSERT_TECHLOG_SESSION",
    "INSERT_TECHLOG_STREAM",
    "MARK_PHANTOM",
    "MARK_SUPERSEDED",
    "SELECT_BACKFILL_THROUGHPUT",
    "SELECT_CALIBRATION_PAIRS",
    "SELECT_COLLECTION_HEALTH",
    "SELECT_CONTEXT_EVENT_ID",
    "SELECT_GAP_MINUTES_BY_REASON",
    "SELECT_LAST_SAMPLE_AT",
    "SELECT_LIBRARY_URIS",
    "SELECT_ME_ID",
    "SELECT_NULL_CONTEXT_RATIO",
    "SELECT_OPEN_SESSIONS",
    "SELECT_ORIGIN_DISTRIBUTION",
    "SELECT_OWN_PLAYLISTS",
    "SELECT_OWN_PLAYLIST_TRACK_URIS",
    "SELECT_PLAYLISTS_NEEDING_REFRESH",
    "SELECT_PLAYLIST_META",
    "SELECT_POLLER_MATCH",
    "SELECT_RP_NEAR",
    "SELECT_SAMPLES_SINCE",
    "SELECT_SESSIONS_FOR_CLASSIFY",
    "SELECT_SESSIONS_NEAR",
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
    "UPSERT_SESSION_CONTEXT_EVIDENCE",
    "UPSERT_TRACK_DURATION_HINT",
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
# 기술 로그 귀속을 LEFT JOIN 한다. 관측된 context_uri 가 있으면 그쪽이 이기고,
# 없을 때만 유추 컨텍스트가 쓰인다 (판단은 recompute 가 한다).
#
# rules_version 으로 조인하지 않고 MAX 를 쓰는 이유: 증거는 임포트 시점의
# 버전으로 박히는데, 이후 RULES_VERSION 이 올라가면 조인이 통째로 끊긴다.
# SQLite 는 MAX() 와 함께 고른 bare column 이 그 최대 행의 값임을 보장한다.
SELECT_SESSIONS_FOR_CLASSIFY = """
SELECT ps.id, ps.track_uri, ps.started_at, ps.context_uri, ps.context_type,
       ps.shuffle, ps.reason_start,
       ps.context_origin AS previous_origin,
       ps.context_origin_rules_version AS rules_version,
       ps.started_at - LAG(ps.ended_at) OVER (ORDER BY ps.started_at, ps.id)
         AS silence_before_ms,
       ev.context_uri  AS inferred_context_uri,
       ev.context_type AS inferred_context_type,
       ev.confidence   AS inferred_confidence
  FROM play_session ps
  LEFT JOIN (
        SELECT session_id, context_uri, context_type, confidence,
               MAX(rules_version) AS v
          FROM session_context_evidence
         WHERE context_type IS NOT NULL
         GROUP BY session_id
       ) ev ON ev.session_id = ps.id
 WHERE ps.superseded_by IS NULL AND ps.suspect_phantom = 0
 ORDER BY ps.started_at, ps.id
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

# 수록곡을 받아 와야 할 내 플레이리스트. Spotify 생성분은 제외한다 —
# `/playlists/{id}` 가 404 라 항목 조회도 실패하고, 애초에 '내가 만든 것'이
# 아니라 자발성 증거가 되지 못한다.
SELECT_OWN_PLAYLISTS = """
SELECT uri, id FROM playlist_meta
 WHERE owner_id = ? AND is_generated = 0 AND fetch_status = 'ok'
 ORDER BY uri
"""

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

# history_export 는 구조적으로 context 가 없다 — 섞으면 '계정 증상' 경고가
# 오탐을 낸다. 이 비율은 context 를 가질 수 *있는* 출처만으로 계산해야 한다.
SELECT_NULL_CONTEXT_RATIO = """
SELECT
  count(*) AS total,
  sum(CASE WHEN context_uri IS NULL THEN 1 ELSE 0 END) AS null_context
FROM v_clean_session
WHERE source != 'history_export'
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
  (SELECT count(*) FROM track  WHERE fetch_status = 'not_found')          AS tracks_missing,
  -- 세션에는 있지만 아직 큐에 등록되지 않은 트랙. enqueue 는 backfill run
  -- 시점에 일어나므로, 이걸 안 보여 주면 대량 임포트 직후 status 가
  -- '대기 0' 이라는 거짓말을 한다.
  (SELECT count(DISTINCT s.track_uri) FROM play_session s
    WHERE NOT EXISTS (SELECT 1 FROM track t WHERE t.uri = s.track_uri)) AS tracks_unqueued
"""

# 실제로 하루에 몇 건이 풀렸는지. 예산(--max-calls)이 아니라 이 값으로
# 완료일을 잡아야 한다 — dev 모드 쿼터 때문에 예산을 다 쓰지 못하고
# rate_limit 로 물러나는 것이 정상이라 예산 기준 추정은 몇 배씩 낙관적이다.
SELECT_BACKFILL_THROUGHPUT = """
SELECT count(*) AS resolved,
       -- 밤 단위로 도는 배치라 KST 일자로 묶는다 (+9h).
       count(DISTINCT date((fetched_at + 32400000) / 1000, 'unixepoch')) AS nights
  FROM (SELECT fetched_at FROM track  WHERE fetch_status = 'ok' AND fetched_at >= :since
        UNION ALL
        SELECT fetched_at FROM artist WHERE fetch_status = 'ok' AND fetched_at >= :since
        UNION ALL
        SELECT fetched_at FROM album  WHERE fetch_status = 'ok' AND fetched_at >= :since)
"""

# 공백은 사유마다 뜻이 전혀 다르다. process_restart 는 크래시일 수도 슬립일
# 수도 있고, error_backoff 는 API 장애, rate_limit 는 정상 동작이다. 합계만
# 보여 주면 사람이 어느 쪽인지 판단할 수 없다.
# 창 **밖에서 시작한** 공백도 겹치는 만큼 센다. `started_at >= :since` 로
# 자르면 7일짜리 단절이 어제 시작했다는 이유만으로 통째로 사라진다 —
# 2026-08-24~08-31 단절이 리포트에 0분으로 찍힌 두 번째 이유가 이것이다.
SELECT_GAP_MINUTES_BY_REASON = """
SELECT reason, sum(min(ended_at, :until) - max(started_at, :since)) AS ms
  FROM collector_gap
 WHERE ended_at > :since AND started_at < :until
 GROUP BY reason ORDER BY ms DESC
"""

# --- 취향 특성 (Phase 2) ---------------------------------------------------

# 모드별 완주 기저율. 파라미터 버전을 만들 때 한 번 계산해 굳혀 넣는다 —
# 데이터가 쌓이면 이 값이 움직이는데, 그때마다 과거 점수의 의미가 소급해
# 바뀌면 재현이 불가능해진다.
SELECT_MODE_BASE_RATES = """
SELECT s.reason_start AS mode, count(*) AS n,
       avg(min(1.0 * s.ms_played / t.duration_ms, 1.0)) AS base_rate
  FROM v_clean_session s JOIN track t ON t.uri = s.track_uri
 WHERE s.ms_played IS NOT NULL AND t.duration_ms > 0
 GROUP BY s.reason_start HAVING n >= 30
"""

SELECT_GLOBAL_BASE_RATE = """
SELECT avg(min(1.0 * s.ms_played / t.duration_ms, 1.0)) AS base_rate
  FROM v_clean_session s JOIN track t ON t.uri = s.track_uri
 WHERE s.ms_played IS NOT NULL AND t.duration_ms > 0
"""

INSERT_FEATURE_PARAMS = """
INSERT INTO feature_params (version, params_json, note, created_at) VALUES (?, ?, ?, ?)
"""

SELECT_FEATURE_PARAMS = "SELECT * FROM feature_params WHERE version = ?"

SELECT_LATEST_FEATURE_PARAMS = "SELECT * FROM feature_params ORDER BY version DESC LIMIT 1"

# 0층 입력. duration 은 카탈로그에서 오므로 백필이 덜 된 트랙은 NULL 이고,
# 그 경우 완주도를 '모른다'로 둔다 (0 이 아니다).
SELECT_SESSIONS_FOR_WEIGHTS = """
SELECT s.id, s.track_uri, s.started_at, s.ms_played, s.completion_ratio,
       s.inferred_completion_ratio, s.reason_start, s.end_reason, s.source,
       s.context_origin, t.duration_ms
  FROM v_clean_session s LEFT JOIN track t ON t.uri = s.track_uri
 ORDER BY s.id
"""

UPSERT_PLAY_WEIGHT = """
INSERT INTO play_weight
  (session_id, params_version, completion, completion_source, mode, mode_score,
   voluntary, weight_current, weight_core, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (session_id, params_version) DO UPDATE SET
  completion = excluded.completion,
  completion_source = excluded.completion_source,
  mode = excluded.mode,
  mode_score = excluded.mode_score,
  voluntary = excluded.voluntary,
  weight_current = excluded.weight_current,
  weight_core = excluded.weight_core,
  computed_at = excluded.computed_at
"""

# 1층. 아티스트는 track_artist 를 지나므로 백필된 트랙만 집계된다 —
# 그 사실이 measured_plays 와 함께 드러나야 점수를 얼마나 믿을지 알 수 있다.
AGGREGATE_ARTIST_AFFINITY = """
SELECT ta.artist_uri AS entity_key,
       sum(w.weight_current * CASE WHEN ta.position = 0 THEN 1.0 ELSE ? END) AS score_current,
       sum(w.weight_core    * CASE WHEN ta.position = 0 THEN 1.0 ELSE ? END) AS score_core,
       count(*) AS plays,
       sum(CASE WHEN w.completion_source = 'measured' THEN 1 ELSE 0 END) AS measured_plays
  FROM play_weight w
  JOIN v_clean_session s ON s.id = w.session_id
  JOIN track_artist ta ON ta.track_uri = s.track_uri
 WHERE w.params_version = ?
 GROUP BY ta.artist_uri
"""

AGGREGATE_TRACK_AFFINITY = """
SELECT s.track_uri AS entity_key,
       sum(w.weight_current) AS score_current,
       sum(w.weight_core) AS score_core,
       count(*) AS plays,
       sum(CASE WHEN w.completion_source = 'measured' THEN 1 ELSE 0 END) AS measured_plays
  FROM play_weight w JOIN v_clean_session s ON s.id = w.session_id
 WHERE w.params_version = ?
 GROUP BY s.track_uri
"""

AGGREGATE_ALBUM_AFFINITY = """
SELECT t.album_uri AS entity_key,
       sum(w.weight_current) AS score_current,
       sum(w.weight_core) AS score_core,
       count(*) AS plays,
       sum(CASE WHEN w.completion_source = 'measured' THEN 1 ELSE 0 END) AS measured_plays
  FROM play_weight w
  JOIN v_clean_session s ON s.id = w.session_id
  JOIN track t ON t.uri = s.track_uri
 WHERE w.params_version = ? AND t.album_uri IS NOT NULL
 GROUP BY t.album_uri
"""

UPSERT_ENTITY_AFFINITY = """
INSERT INTO entity_affinity
  (entity_type, entity_key, horizon, params_version, score, plays, measured_plays, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (entity_type, entity_key, horizon, params_version) DO UPDATE SET
  score = excluded.score,
  plays = excluded.plays,
  measured_plays = excluded.measured_plays,
  computed_at = excluded.computed_at
"""

SELECT_TOP_AFFINITY = """
SELECT a.entity_key, a.score, a.plays, a.measured_plays,
       COALESCE(ar.name, tr.name, al.name) AS name
  FROM entity_affinity a
  LEFT JOIN artist ar ON ar.uri = a.entity_key
  LEFT JOIN track  tr ON tr.uri = a.entity_key
  LEFT JOIN album  al ON al.uri = a.entity_key
 WHERE a.entity_type = ? AND a.horizon = ? AND a.params_version = ?
 ORDER BY a.score DESC LIMIT ?
"""

# 3층 입력. 0층이 이미 완주도와 그 출처를 굳혀 뒀으므로 여기서 다시 풀지
# 않는다 — 같은 재생이 두 곳에서 다르게 해석되면 층끼리 말이 안 맞는다.
# 시/요일은 KST 로 여기서 낸다. `local_date` 생성 컬럼과 같은 관례이고,
# 이렇게 해야 계산 쪽(`features/behavior.py`)이 시간대를 몰라도 된다.
# ORDER BY started_at 은 '처음 듣는 곡' 판정의 전제다. 바꾸지 말 것.
SELECT_SESSIONS_FOR_BEHAVIOR = """
SELECT s.track_uri, w.completion, w.completion_source, s.context_origin,
       CAST(strftime('%H', s.started_at / 1000, 'unixepoch', '+9 hours') AS INTEGER) AS kst_hour,
       CAST(strftime('%w', s.started_at / 1000, 'unixepoch', '+9 hours') AS INTEGER) AS kst_dow
  FROM play_weight w JOIN v_clean_session s ON s.id = w.session_id
 WHERE w.params_version = ?
 ORDER BY s.started_at, s.id
"""

UPSERT_BEHAVIOR_PROFILE = """
INSERT INTO behavior_profile
  (period, metric, source_filter, params_version, value, sample_size, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (period, metric, source_filter, params_version) DO UPDATE SET
  value = excluded.value,
  sample_size = excluded.sample_size,
  computed_at = excluded.computed_at
"""

# 재계산이 지표를 지우는 경우(정의가 빠지거나 기간이 사라짐)에도 낡은 행이
# 남지 않게 한다. 같은 params_version 안에서만 지운다 — 다른 버전은 비교
# 대상이므로 건드리면 안 된다.
DELETE_BEHAVIOR_PROFILE = "DELETE FROM behavior_profile WHERE params_version = ?"

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


# ===========================================================================
# 기술 로그 임포트 (마이그레이션 0004)
# ===========================================================================

INSERT_IMPORT_BATCH = """
INSERT INTO import_batch
  (kind, source_path, window_start, window_end, files_json, stats_json, imported_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
"""

# 붙일 후보를 시각 창 안에서 가까운 순으로. ms_played 조건은 호출부가 출처별로
# 다르게 적용한다 — 폴러는 독립 측정이라 최대 18초까지 벌어지고 RP 는 NULL 이다.
SELECT_SESSIONS_NEAR = """
SELECT id, source, started_at, ms_played
  FROM play_session
 WHERE track_uri = ? AND superseded_by IS NULL
   AND abs(started_at - ?) <= ?
 ORDER BY abs(started_at - ?)
"""

# 기술 로그에만 있는 재생(46건). context 는 채우지 않는다 — 유추는 증거
# 테이블로 가고, 이 컬럼은 관측값 자리다.
INSERT_TECHLOG_SESSION = """
INSERT INTO play_session
  (track_uri, started_at, ended_at, ms_played, device_type, shuffle,
   source, timing_confidence, end_reason, reason_start, is_open,
   created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, 'techlog', 'high', ?, ?, 0, ?, ?)
ON CONFLICT (track_uri, started_at) DO NOTHING
"""

INSERT_TECHLOG_STREAM = """
INSERT INTO techlog_stream
  (batch_id, ended_at, track_uri, ms_played, reason_start, reason_end, shuffle,
   media_type, is_downloaded, social_listening, displayed_track_uri, audio_id,
   device_type, device_model, os_name, os_version, app_version, conn_country,
   playback_id, est_total_ms, n_seekback, ms_seekback, n_seekfwd, ms_seekfwd,
   n_stutter, boombox_gap_ms, session_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (track_uri, ended_at) DO NOTHING
"""

INSERT_CONTEXT_EVENT = """
INSERT INTO techlog_context_event
  (batch_id, emitted_at, kind, context_uri, source_start, trigger, algorithm,
   context_length, track_uri, sequence_uuid, event_key)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (event_key) DO NOTHING
"""

# 충돌로 삽입이 건너뛰어졌을 때 기존 행의 id 를 찾는다 (재임포트 경로).
SELECT_CONTEXT_EVENT_ID = """
SELECT id FROM techlog_context_event WHERE event_key = ?
"""

INSERT_CONTEXT_ITEM = """
INSERT INTO techlog_context_item (event_id, position, track_uri, original_position)
VALUES (?, ?, ?, ?)
ON CONFLICT (event_id, position) DO NOTHING
"""

# 규칙이 바뀌면 같은 (session_id, rules_version) 을 덮어쓴다 — 재계산 가능해야
# 하므로 이력이 아니라 현재값이다.
UPSERT_SESSION_CONTEXT_EVIDENCE = """
INSERT INTO session_context_evidence
  (session_id, rules_version, context_uri, context_type, method, confidence,
   event_id, lag_ms, computed_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (session_id, rules_version) DO UPDATE SET
  context_uri = excluded.context_uri,
  context_type = excluded.context_type,
  method = excluded.method,
  confidence = excluded.confidence,
  event_id = excluded.event_id,
  lag_ms = excluded.lag_ms,
  computed_at = excluded.computed_at
"""

INSERT_ALGO_EXPOSURE = """
INSERT INTO algo_exposure
  (batch_id, observed_at, surface, context_uri, title, position, meta_json, event_key)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (event_key) DO NOTHING
"""

INSERT_ALGO_EXPOSURE_ITEM = """
INSERT INTO algo_exposure_item (exposure_id, position, item_uri)
VALUES (?, ?, ?)
ON CONFLICT (exposure_id, position) DO NOTHING
"""

UPSERT_TRACK_DURATION_HINT = """
INSERT INTO track_duration_hint
  (track_uri, duration_ms, source, observations, disagreements, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT (track_uri) DO UPDATE SET
  duration_ms = excluded.duration_ms,
  observations = excluded.observations,
  disagreements = excluded.disagreements
"""
