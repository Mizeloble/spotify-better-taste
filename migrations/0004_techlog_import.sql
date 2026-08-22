-- Technical Log Information (Spotify 개인정보 페이지) 임포트용 스키마.
--
-- 설계 근거 전체는 docs/techlog-import-schema.md 에 있다. 여기에는 왜 이
-- 모양이어야 하는지만 남긴다.
--
-- 이 export 는 재생 *사실*을 늘리지 않는다 — RawCoreStream 4,146행 중 4,063행이
-- 이미 history_export 로 들어와 있다. 새로 얻는 것은 셋뿐이다:
--   1. 컨텍스트 (export 39,006행 전부가 context_uri NULL 이다)
--   2. 곡 길이 (백필이 못 간 692곡을 API 호출 0회로)
--   3. 알고리즘 노출 ("무엇을 밀어 줬나" — Extended History 에 원리적으로 없다)

-- ===========================================================================
-- 배치 증적
-- ===========================================================================

-- 어떤 export 파일에서 무엇이 들어왔나. 파일 자체는 DB 밖에 두므로(151개
-- 180MB) 재현성은 이 행이 진다. 파일별 sha256 이 같으면 같은 원본이다.
CREATE TABLE import_batch (
  id           INTEGER PRIMARY KEY,
  kind         TEXT    NOT NULL,   -- 'techlog' | 'extended_history'
  source_path  TEXT    NOT NULL,
  window_start INTEGER,            -- 로그가 실제로 덮는 구간
  window_end   INTEGER,
  files_json   TEXT    NOT NULL,   -- [{name, sha256, bytes, rows}]
  stats_json   TEXT,
  imported_at  INTEGER NOT NULL
);

-- ===========================================================================
-- 재생 로그 — RawCoreStream 1줄 = 1행
-- ===========================================================================

-- play_session 에 자리가 없는 필드를 원형 그대로 보관한다. 해석 금지.
-- session_id 는 매칭 결과이고 NULL 이면 아직 못 붙였다는 뜻이다.
CREATE TABLE techlog_stream (
  id                  INTEGER PRIMARY KEY,
  batch_id            INTEGER NOT NULL REFERENCES import_batch(id),
  ended_at            INTEGER NOT NULL,   -- timestamp_utc (재생이 끝난 시각)
  track_uri           TEXT    NOT NULL,
  ms_played           INTEGER NOT NULL,
  reason_start        TEXT,
  reason_end          TEXT,
  shuffle             INTEGER,
  media_type          TEXT,               -- audio|video|unknown
  is_downloaded       INTEGER,
  social_listening    INTEGER,
  -- 관계 연결(relinking) 흔적. 기록만 하고 매칭에는 쓰지 않는다 — 다른 행
  -- 185건을 대조해 보니 DB 는 185건 전부를 content_uri 쪽으로 갖고 있었다.
  displayed_track_uri TEXT,
  audio_id            TEXT,

  device_type         TEXT,               -- mobile|desktop
  device_model        TEXT,
  os_name             TEXT,
  os_version          TEXT,
  app_version         TEXT,
  conn_country        TEXT,               -- 여행 구간이 여기서 드러난다

  -- BoomboxPlaybackSession 시각 조인 결과.
  playback_id         TEXT,
  est_total_ms        INTEGER,            -- 곡 길이. 검증 결과 API 값과 동일
  n_seekback          INTEGER,
  ms_seekback         INTEGER,
  n_seekfwd           INTEGER,
  ms_seekfwd          INTEGER,
  n_stutter           INTEGER,
  -- 실측 시차는 중앙값 45ms · p95 112ms 다. 5초씩 열어 두면 5초 내 후보가
  -- 둘 이상인 288건에서 오결합이 난다. 1,000ms 초과면 Boombox 값을 안 쓴다.
  boombox_gap_ms      INTEGER,

  session_id          INTEGER REFERENCES play_session(id),
  UNIQUE (track_uri, ended_at)
);
CREATE INDEX idx_tls_session ON techlog_stream(session_id);
CREATE INDEX idx_tls_ended   ON techlog_stream(ended_at);

-- ===========================================================================
-- 컨텍스트 증거 — 원본
-- ===========================================================================

-- 서로 다른 로그를 "이 시각에 어떤 컨텍스트가 살아 있었나" 하나의 스트림으로
-- 정규화한다. 스키마는 다르지만 답하는 질문이 같다.
--
-- AdvanceStuck 은 일부러 뺐다. source_start 가 'playlist' 처럼 컨텍스트의
-- *종류*만 주고 context_uri 는 없어 어느 플레이리스트인지 특정할 수 없다.
CREATE TABLE techlog_context_event (
  id             INTEGER PRIMARY KEY,
  batch_id       INTEGER NOT NULL REFERENCES import_batch(id),
  emitted_at     INTEGER NOT NULL,
  kind           TEXT    NOT NULL,  -- 'shuffle_sequence' | 'track_not_played'
  context_uri    TEXT,
  source_start   TEXT,              -- playlist|home|artist|driving-jumpstart …
  trigger        TEXT,              -- context_update|play|toggled_shuffle_on
  algorithm      TEXT,              -- FRESH
  context_length INTEGER,           -- 셔플 대상 컨텍스트의 곡 수
  track_uri      TEXT,              -- 단건 이벤트일 때만
  sequence_uuid  TEXT,
  -- 멱등성 키. NULL 이 섞인 컬럼 조합을 UNIQUE 로 쓰면 안 된다 — SQLite 는
  -- UNIQUE 안의 NULL 을 서로 다른 값으로 보므로 재임포트가 행을 두 배로
  -- 만든다 (TrackNotPlayed 379행은 sequence_uuid 가 전부 NULL 이다).
  -- 원본 JSON 객체를 키 정렬해 직렬화한 sha256 이 들어간다.
  event_key      TEXT    NOT NULL UNIQUE
);
CREATE INDEX idx_tce_time ON techlog_context_event(emitted_at);

-- 셔플 시퀀스의 트랙 목록. original_position 은 원본 컨텍스트에서의 위치라
-- "셔플이 얼마나 멀리 뛰었나"까지 나중에 볼 수 있다.
CREATE TABLE techlog_context_item (
  event_id          INTEGER NOT NULL REFERENCES techlog_context_event(id),
  position          INTEGER NOT NULL,
  track_uri         TEXT    NOT NULL,
  original_position INTEGER,
  PRIMARY KEY (event_id, position)
);
CREATE INDEX idx_tci_track ON techlog_context_item(track_uri);

-- ===========================================================================
-- 컨텍스트 귀속 — 파생
-- ===========================================================================

-- 재생 하나에 컨텍스트를 붙인 결과.
--
-- play_session.context_uri 에 쓰지 않는 이유: 그 컬럼은 /me/player 가 실제로
-- 관측한 값 자리다. 유추를 같은 칸에 넣으면 실측과 추정이 섞인다 (규칙 3).
--
-- rules_version 이 PK 에 있어야 규칙 개정 전후를 나란히 놓고 비교할 수 있다
-- (entity_affinity 의 params_version 과 같은 장치).
--
-- 정확도는 84.5% 다 (TrackNotPlayed.play_context 대조, n=58). 표본이 작아
-- confidence 없이 쓰면 안 된다.
CREATE TABLE session_context_evidence (
  session_id    INTEGER NOT NULL REFERENCES play_session(id),
  rules_version INTEGER NOT NULL,
  context_uri   TEXT,
  context_type  TEXT,              -- playlist|collection|album|artist|single_track
  method        TEXT    NOT NULL,  -- 'shuffle_sequence' | 'none'
  confidence    TEXT    NOT NULL,  -- high|medium|low
  event_id      INTEGER REFERENCES techlog_context_event(id),
  lag_ms        INTEGER,           -- 증거 이벤트와 재생 사이의 시간차
  computed_at   INTEGER NOT NULL,
  PRIMARY KEY (session_id, rules_version)
);
CREATE INDEX idx_sce_context ON session_context_evidence(context_uri);

-- ===========================================================================
-- 알고리즘 노출
-- ===========================================================================

-- "무엇을 밀어 줬나". Daylist/OnRepeat/ReleaseRadar/Continuum 을 한 모양으로
-- 담는다. surface 별로 테이블을 쪼개면 "노출 대비 재생률"을 물을 때마다
-- UNION 을 쓰게 된다.
CREATE TABLE algo_exposure (
  id          INTEGER PRIMARY KEY,
  batch_id    INTEGER NOT NULL REFERENCES import_batch(id),
  observed_at INTEGER NOT NULL,
  surface     TEXT    NOT NULL,   -- daylist|on_repeat|release_radar|continuum
  context_uri TEXT,               -- 플레이리스트/믹스/섹션 uri
  title       TEXT,
  position    INTEGER,            -- 피드 안에서의 순위
  meta_json   TEXT,               -- daylist: seed/anti_seed/daypart/concepts
  -- 위와 같은 이유로 내용 해시를 쓴다. (surface, observed_at, context_uri) 를
  -- UNIQUE 로 걸면 Continuum 15행 + ReleaseRadar 6행이 조용히 사라지고,
  -- context_uri 가 아예 없는 daylist 는 재임포트마다 중복된다.
  event_key   TEXT    NOT NULL UNIQUE
);
CREATE INDEX idx_ae_time ON algo_exposure(observed_at);

CREATE TABLE algo_exposure_item (
  exposure_id INTEGER NOT NULL REFERENCES algo_exposure(id),
  position    INTEGER NOT NULL,
  item_uri    TEXT    NOT NULL,
  PRIMARY KEY (exposure_id, position)
);
CREATE INDEX idx_aei_item ON algo_exposure_item(item_uri);

-- ===========================================================================
-- 곡 길이 힌트
-- ===========================================================================

-- track.duration_ms 는 API 관측 전용으로 남긴다. 로그에서 얻은 길이는 여기.
--
-- 이름은 '_est' 지만 추정이 아니다 — 이미 백필된 1,269곡과 대조한 결과 오차
-- 중앙값 0ms, p90 0ms 였다. 그래도 카탈로그에 섞지 않는 이유는 출처가 다르면
-- 분리해 두어야 나중에 "실측만으로 다시 계산"이 가능하기 때문이다 (규칙 3).
--
-- verified_delta_ms 는 나중에 백필이 그 곡에 닿았을 때 채워지는 사후 검증값이고,
-- 이 컬럼이 있어야 "힌트를 계속 믿어도 되나"에 데이터로 답할 수 있다.
CREATE TABLE track_duration_hint (
  track_uri         TEXT PRIMARY KEY,
  duration_ms       INTEGER NOT NULL,
  source            TEXT    NOT NULL,   -- 'techlog_boombox'
  observations      INTEGER NOT NULL,   -- 같은 곡을 몇 번 관측했나
  -- 값이 갈리는 곡이 실제로 3개 있었다 (최대 155초 차이 — 다른 버전이거나
  -- 오결합이다). disagreements > 0 이면 힌트를 채택하지 않는다. 1,961곡 중
  -- 3곡을 버리는 대가로 조용한 오염을 막는다.
  disagreements     INTEGER NOT NULL,
  verified_delta_ms INTEGER,
  created_at        INTEGER NOT NULL
);
