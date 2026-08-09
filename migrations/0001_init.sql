-- spotify-taste 초기 스키마 (Phase 1~6 전체).
--
-- 왜 전 Phase 를 한 번에 만드나: 빈 테이블은 공짜이고, 나중에 되돌릴 수 없는
-- 설계 결정(대리키, rules_version, is_generated, track_tag 의 복합 PK)을 지금
-- 강제하기 위해서다. 이 중 하나라도 빠지면 Phase 2~6 에서 선택지가 닫힌다.
--
-- 시간은 예외 없이 UTC epoch **밀리초 정수**다.
-- 마이그레이션 러너가 이 파일 전체를 한 트랜잭션에서 적용하고 user_version 을
-- 올린다. 파일 안에서 PRAGMA user_version 을 직접 건드리지 않는다.

-- ===========================================================================
-- 출처 / 원본 아카이브
-- ===========================================================================

-- 2계층 아카이브의 바깥층. 저용량 엔드포인트는 전량 영구 보관하고,
-- /me/player 는 세션 첫 샘플 · 1/N 카나리 · 처음 보는 응답 모양만 보관한다.
CREATE TABLE raw_api_response (
  id             INTEGER PRIMARY KEY,
  fetched_at     INTEGER NOT NULL,
  endpoint       TEXT    NOT NULL,   -- '/me/player', '/tracks/{id}' 처럼 템플릿 형태
  request_params TEXT,               -- JSON
  http_status    INTEGER NOT NULL,
  body           TEXT,               -- 원문 그대로. 가공 금지
  body_sha256    TEXT,
  shape_hash     TEXT,               -- 정렬된 JSON 키패스 목록의 sha256
  source_job     TEXT    NOT NULL,   -- poller|recent|backfill|snapshot|cli
  keep_until     INTEGER             -- NULL = 영구 보관
);
CREATE INDEX idx_raw_endpoint_time ON raw_api_response(endpoint, fetched_at);
CREATE INDEX idx_raw_keep_until    ON raw_api_response(keep_until)
  WHERE keep_until IS NOT NULL;

-- 엔드포인트별로 관측된 응답 "모양"의 목록. 처음 보는 shape_hash 가 들어오면
-- 알림이 나간다 — API 축소가 계속되는 상황에서 필드 삭제를 실시간으로 잡는다.
CREATE TABLE api_schema_shape (
  endpoint      TEXT    NOT NULL,
  shape_hash    TEXT    NOT NULL,
  key_paths     TEXT    NOT NULL,   -- JSON 배열
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  sample_raw_id INTEGER REFERENCES raw_api_response(id),
  alerted       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (endpoint, shape_hash)
);

-- 2계층 아카이브의 안쪽층이자 세션 재구성의 재현 가능한 원천. 영구 보관.
-- 행당 ~100바이트라 몇 년치를 쌓아도 부담이 없다.
CREATE TABLE player_sample (
  id                     INTEGER PRIMARY KEY,
  sampled_at             INTEGER NOT NULL,  -- 응답 수신 시점의 로컬 시계
  http_status            INTEGER NOT NULL,  -- 200 | 204
  is_playing             INTEGER,
  track_uri              TEXT,
  duration_ms            INTEGER,
  progress_ms            INTEGER,
  context_uri            TEXT,
  context_type           TEXT,
  device_id              TEXT,
  device_type            TEXT,
  device_is_private      INTEGER,
  shuffle_state          INTEGER,
  repeat_state           TEXT,
  currently_playing_type TEXT,
  -- API 자체의 timestamp. 기기에서 재생 상태가 마지막으로 바뀐 시각이고 기기
  -- 시계는 흐르므로 산술에 쓰지 않는다. 드리프트 연구용으로만 보관.
  spotify_timestamp      INTEGER,
  raw_id                 INTEGER REFERENCES raw_api_response(id)
);
CREATE INDEX idx_sample_time       ON player_sample(sampled_at);
CREATE INDEX idx_sample_track_time ON player_sample(track_uri, sampled_at);

-- 수집이 끊긴 구간. recently-played 조정기에게 "어디를 봐야 하는지" 알려준다.
CREATE TABLE collector_gap (
  id            INTEGER PRIMARY KEY,
  started_at    INTEGER NOT NULL,
  ended_at      INTEGER NOT NULL,
  reason        TEXT    NOT NULL,  -- process_restart|error_backoff|rate_limit|auth
  reconciled_at INTEGER
);
CREATE INDEX idx_gap_time ON collector_gap(started_at);

-- ===========================================================================
-- 핵심 사실 테이블
-- ===========================================================================

CREATE TABLE play_session (
  -- 대리키. 병합이 started_at 을 고쳐 써도 살아남는 안정적 핸들이 필요하다
  -- (recommendation_result 등 이후 조인이 여기 붙는다).
  id                        INTEGER PRIMARY KEY,
  track_uri                 TEXT    NOT NULL,
  started_at                INTEGER NOT NULL,
  ended_at                  INTEGER,

  -- 실측. 폴러만 채운다. recently_played 출처는 반드시 NULL.
  ms_played                 INTEGER,
  duration_ms               INTEGER,  -- 재생 당시 관측값 (relinking 으로 바뀔 수 있음)
  max_progress_ms           INTEGER,
  -- 위치 기반: 재생 헤드가 트랙의 어디까지 갔나. 스킵 판정에 쓴다.
  completion_ratio          REAL,
  -- 시간 기반: 실제로 얼마나 들었나. 80% 지점으로 스크럽 후 끝까지 두면
  -- completion 은 1.0 이지만 listened 는 0.2 다. 둘은 다른 질문에 답한다.
  listened_ratio            REAL,
  -- played_at 간격에서 역산한 추정치. 위 둘과 절대 섞지 않는다.
  inferred_completion_ratio REAL,

  context_uri               TEXT,
  context_type              TEXT,     -- playlist|album|artist|collection|show|NULL
  device_type               TEXT,
  device_id                 TEXT,
  device_is_private         INTEGER,
  shuffle                   INTEGER,

  source                    TEXT    NOT NULL,  -- poller|recently_played|history_export
  timing_confidence         TEXT    NOT NULL,  -- high(poller) | low(rp, 30초 반올림)
  end_reason                TEXT,              -- track_change|rewind|pause_timeout
                                               -- |idle_timeout|process_restart|rp_inferred
  is_open                   INTEGER NOT NULL DEFAULT 0,
  confirmed_by_rp           INTEGER NOT NULL DEFAULT 0,
  suspect_phantom           INTEGER NOT NULL DEFAULT 0,
  -- 중복은 지우지 않고 대체 표시만 한다. 원본이 아카이브돼 있고 규칙은
  -- 언제든 재계산 가능하므로, 삭제는 정보를 잃기만 한다.
  superseded_by             INTEGER REFERENCES play_session(id),

  sample_count              INTEGER,
  seek_forward_count        INTEGER NOT NULL DEFAULT 0,
  first_sample_id           INTEGER,
  last_sample_id            INTEGER,

  -- 파생. classify/ 만 기록한다. 폴러가 여기 쓰면 버그다.
  context_origin               TEXT,     -- self|algorithmic|external|unknown
  context_origin_reason        TEXT,
  context_origin_rules_version INTEGER,

  created_at                INTEGER NOT NULL,
  updated_at                INTEGER NOT NULL,
  -- KST 는 DST 가 없어 결정적이다. STORED 라 일자 그룹핑이 인덱스를 탄다.
  local_date TEXT GENERATED ALWAYS AS
    (date(started_at / 1000, 'unixepoch', '+9 hours')) STORED,

  UNIQUE (track_uri, started_at)
);
CREATE INDEX idx_ps_started     ON play_session(started_at);
CREATE INDEX idx_ps_local_date  ON play_session(local_date);
CREATE INDEX idx_ps_origin      ON play_session(context_origin, started_at);
CREATE INDEX idx_ps_context     ON play_session(context_uri);
CREATE INDEX idx_ps_track_time  ON play_session(track_uri, started_at);
CREATE INDEX idx_ps_open        ON play_session(is_open) WHERE is_open = 1;
CREATE INDEX idx_ps_stale_rules ON play_session(context_origin_rules_version);

-- 모든 학습·평가 쿼리는 이 뷰만 쓴다. 대체·팬텀 제외 조건을 호출부가 잊을 수
-- 없게 만드는 것이 목적이다.
CREATE VIEW v_clean_session AS
  SELECT * FROM play_session
   WHERE superseded_by IS NULL AND suspect_phantom = 0;

-- ===========================================================================
-- 카탈로그 — 복수 조회가 사라져 1엔티티=1콜이다. fetch_status 가 곧 백필 큐.
-- ===========================================================================

CREATE TABLE track (
  uri            TEXT PRIMARY KEY,
  id             TEXT,
  name           TEXT,
  album_uri      TEXT,
  duration_ms    INTEGER,
  explicit       INTEGER,
  isrc           TEXT,               -- 국가 접두사가 남은 몇 안 되는 출처 신호
  disc_number    INTEGER,
  track_number   INTEGER,
  is_local       INTEGER,
  fetched_at     INTEGER,
  fetch_status   TEXT    NOT NULL DEFAULT 'pending',  -- pending|ok|not_found|error
  http_status    INTEGER,
  fetch_attempts INTEGER NOT NULL DEFAULT 0,
  raw_id         INTEGER REFERENCES raw_api_response(id)
);
CREATE INDEX idx_track_fetch ON track(fetch_status);

CREATE TABLE album (
  uri                    TEXT PRIMARY KEY,
  id                     TEXT,
  name                   TEXT,
  album_type             TEXT,
  -- popularity 가 제거된 지금 era 를 알 수 있는 유일한 신호다.
  release_date           TEXT,
  release_date_precision TEXT,
  label                  TEXT,
  total_tracks           INTEGER,
  genres_json            TEXT,
  fetched_at             INTEGER,
  fetch_status           TEXT    NOT NULL DEFAULT 'pending',
  http_status            INTEGER,
  fetch_attempts         INTEGER NOT NULL DEFAULT 0,
  raw_id                 INTEGER REFERENCES raw_api_response(id)
);
CREATE INDEX idx_album_fetch ON album(fetch_status);

CREATE TABLE artist (
  uri            TEXT PRIMARY KEY,
  id             TEXT,
  name           TEXT,
  -- audio-features 가 죽은 뒤 남은 취향 벡터의 주 재료.
  genres_json    TEXT,
  image_url      TEXT,
  fetched_at     INTEGER,
  fetch_status   TEXT    NOT NULL DEFAULT 'pending',
  http_status    INTEGER,
  fetch_attempts INTEGER NOT NULL DEFAULT 0,
  raw_id         INTEGER REFERENCES raw_api_response(id)
);
CREATE INDEX idx_artist_fetch ON artist(fetch_status);

CREATE TABLE track_artist (
  track_uri  TEXT    NOT NULL REFERENCES track(uri),
  artist_uri TEXT    NOT NULL REFERENCES artist(uri),
  position   INTEGER NOT NULL,
  PRIMARY KEY (track_uri, position)
);
CREATE INDEX idx_ta_artist ON track_artist(artist_uri);

-- ===========================================================================
-- 분류 증거
-- ===========================================================================

CREATE TABLE me (
  id           TEXT PRIMARY KEY,
  display_name TEXT,
  country      TEXT,
  product      TEXT,      -- 'premium' 이어야 /me/player 가 신뢰 가능하다
  fetched_at   INTEGER
);

CREATE TABLE playlist_meta (
  uri                TEXT PRIMARY KEY,
  id                 TEXT,
  name               TEXT,
  description        TEXT,
  owner_id           TEXT,
  owner_display_name TEXT,
  owner_uri          TEXT,
  is_collaborative   INTEGER,
  is_public          INTEGER,
  snapshot_id        TEXT,
  total_items        INTEGER,
  -- 내가 만든 추천 플레이리스트 표식. 없으면 Phase 4 산출물이 owner==me 규칙으로
  -- self 가 되어 Phase 2 학습셋에 재유입되고, 오염원이 곧 평가 데이터라
  -- Phase 5 가 그걸 탐지할 수 없다.
  is_generated       INTEGER NOT NULL DEFAULT 0,
  fetched_at         INTEGER NOT NULL,
  fetch_status       TEXT    NOT NULL,   -- ok|not_found|forbidden|error
  http_status        INTEGER,
  -- TTL. 실패도 캐시한다 — 안 그러면 삭제된 플레이리스트를 매 분류마다
  -- 영원히 재조회한다. ok=+30d, not_found/forbidden=+7d, error=+6h.
  refresh_after      INTEGER NOT NULL,
  raw_id             INTEGER REFERENCES raw_api_response(id)
);
CREATE INDEX idx_pl_refresh ON playlist_meta(refresh_after);
CREATE INDEX idx_pl_owner   ON playlist_meta(owner_id);

CREATE TABLE playlist_item (
  playlist_uri TEXT    NOT NULL,
  track_uri    TEXT    NOT NULL,
  position     INTEGER,
  added_at     INTEGER,
  added_by     TEXT,
  snapshot_id  TEXT    NOT NULL,
  observed_at  INTEGER NOT NULL,
  PRIMARY KEY (playlist_uri, snapshot_id, track_uri)
);
CREATE INDEX idx_pli_track ON playlist_item(track_uri);

CREATE TABLE library_track (
  track_uri   TEXT PRIMARY KEY,
  added_at    INTEGER,
  observed_at INTEGER NOT NULL,
  removed_at  INTEGER            -- 관측되지 않게 된 시점. 행은 지우지 않는다
);

-- /me/top 은 현재 상태만 준다. 스냅샷을 안 찍은 주는 영구 소실이므로
-- 1일차부터 주간으로 쌓는다.
CREATE TABLE top_item (
  item_type   TEXT    NOT NULL,   -- artist|track
  time_range  TEXT    NOT NULL,   -- short_term|medium_term|long_term
  captured_on TEXT    NOT NULL,   -- YYYY-MM-DD (KST)
  rank        INTEGER NOT NULL,
  uri         TEXT    NOT NULL,
  PRIMARY KEY (item_type, time_range, captured_on, rank)
);

-- 관측으로 학습한 상수. 지금은 played_at 이 재생 시작인지 종료인지를 담는다.
CREATE TABLE calibration (
  key         TEXT PRIMARY KEY,
  value       TEXT    NOT NULL,
  sample_size INTEGER,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE sync_cursor (
  name       TEXT PRIMARY KEY,   -- 'recently_played_after' 등
  value      TEXT    NOT NULL,
  updated_at INTEGER NOT NULL
);

-- ===========================================================================
-- Phase 2~6 — 지금은 비어 있지만 지금 만든다
-- ===========================================================================

-- 삭제된 audio-features 의 대체재. LLM 이 추론한 합성 태그.
CREATE TABLE track_tag (
  track_uri            TEXT    NOT NULL REFERENCES track(uri),
  mood                 TEXT,
  era                  TEXT,
  vocal                TEXT,
  energy               TEXT,
  instrumentation_json TEXT,
  confidence           REAL,     -- 낮으면 폐기 (비주류 곡 환각 방어)
  model                TEXT    NOT NULL,
  prompt_version       INTEGER NOT NULL,
  created_at           INTEGER NOT NULL,
  -- model/prompt_version 이 PK 에 들어가야 재태깅이 덮어쓰기가 아니라 추가가
  -- 되고, 프롬프트 개정을 A/B 비교할 수 있다.
  PRIMARY KEY (track_uri, model, prompt_version)
);

CREATE TABLE taste_snapshot (
  id            INTEGER PRIMARY KEY,
  created_at    INTEGER NOT NULL,
  window_days   INTEGER,
  -- 어떤 분류 규칙이 이 스냅샷을 만들었는지. 없으면 Phase 5 의 베이스라인
  -- 비교가 재현 불가능해진다.
  rules_version INTEGER,
  vector_json   TEXT    NOT NULL,
  stats_json    TEXT
);

CREATE TABLE recommendation (
  id                  INTEGER PRIMARY KEY,
  created_at          INTEGER NOT NULL,
  context_label       TEXT,              -- 'weekday_morning' 등
  playlist_uri        TEXT,
  prompt_hash         TEXT    NOT NULL,
  model               TEXT    NOT NULL,
  prompt_version      INTEGER,
  input_snapshot_json TEXT,
  cost_usd            REAL
);

CREATE TABLE recommendation_item (
  rec_id       INTEGER NOT NULL REFERENCES recommendation(id),
  track_uri    TEXT    NOT NULL,
  position     INTEGER NOT NULL,
  slot_type    TEXT    NOT NULL,   -- core|adjacent|explore
  rationale    TEXT,
  -- LLM 이 실제로 말한 원문. /search 해석 실패율이 20~30% 로 예상되므로
  -- 실패를 조용히 버리지 않고 여기에 남겨 나중에 원인을 본다.
  llm_artist   TEXT,
  llm_title    TEXT,
  resolved_via TEXT,               -- exact|search|failed
  PRIMARY KEY (rec_id, track_uri)
);
CREATE UNIQUE INDEX idx_ri_slot ON recommendation_item(rec_id, position);

CREATE TABLE recommendation_result (
  rec_id                INTEGER NOT NULL,
  track_uri             TEXT    NOT NULL,
  first_played_at       INTEGER,
  best_completion_ratio REAL,
  best_listened_ratio   REAL,
  play_count            INTEGER NOT NULL DEFAULT 0,
  saved                 INTEGER NOT NULL DEFAULT 0,
  evaluated_at          INTEGER,
  PRIMARY KEY (rec_id, track_uri)
);
