-- Phase 2 취향 특성. 0001 이 만들어 둔 taste_snapshot/track_tag 는 그대로 쓰고,
-- 여기서는 그 사이를 채우는 계산 계층을 만든다.
--
-- 왜 새로 필요한가:
--   - `artist.genres` 가 API 에서 사라져(CLAUDE.md) 장르를 외부에서 가져와야
--     하는데, 그걸 담을 자리가 없었다.
--   - `taste_snapshot.vector_json` 은 통짜 JSON 이라 "친화도 상위 아티스트"
--     같은 질문에 SQL 로 답할 수 없다. 스냅샷은 불변 기록으로 남기고,
--     질의 가능한 정규화 테이블을 따로 둔다.
--   - 실측/추정/export 를 섞지 않으려면(규칙 3) 계산 결과마다 출처가 남아야
--     한다. JSON 안에서는 그걸 강제할 수 없다.

-- 가중치 파라미터의 버전 레지스트리.
--
-- 반감기·모드 계수·기저율이 바뀌면 아래 모든 파생 행의 의미가 바뀐다. 그래서
-- 파생 테이블은 전부 params_version 을 PK 에 넣고, 그 버전이 정확히 어떤
-- 값이었는지를 여기에 박아 둔다. 이게 없으면 6개월 뒤 "이 점수는 어떤
-- 반감기로 계산된 건가"에 답할 수 없다.
--
-- params_json 에는 손으로 정한 값(반감기 6/24개월 등)뿐 아니라 **데이터에서
-- 유도한 값(모드별 완주 기저율)도 함께 굳혀 넣는다**. 기저율은 데이터가
-- 쌓이면 움직이는데, 그때마다 과거 점수의 의미가 소급해 바뀌면 안 된다.
CREATE TABLE feature_params (
  version     INTEGER PRIMARY KEY,
  params_json TEXT    NOT NULL,
  note        TEXT,
  created_at  INTEGER NOT NULL
);

-- 외부 장르·태그 원본 (MusicBrainz, Last.fm).
--
-- **정규화하지 않고 원본 그대로 넣는다.** Last.fm 상위 태그에는 아티스트
-- 이름(`John Novello`), 국가(`USA`), 악기(`guitar`) 가 섞여 오는데, 무엇을
-- 버릴지는 코드의 정규화 계층이 정한다. 기준을 바꿔도 원본이 남아 있으면
-- 다시 계산할 수 있다 — 행을 지우지 않는다는 원칙과 같은 이유다.
--
-- 아티스트 단위인 이유: 트랙 단위 ISRC 조회는 MusicBrainz 등록이 성겨
-- 36% 에 그쳤고(2026-08-15 실측), 취향 축으로 쓸 것은 어차피 아티스트다.
CREATE TABLE artist_tag (
  artist_uri TEXT    NOT NULL REFERENCES artist(uri),
  source     TEXT    NOT NULL,          -- 'musicbrainz' | 'lastfm'
  tag        TEXT    NOT NULL,          -- 원본 문자열. 소문자화도 하지 않는다
  kind       TEXT,                      -- mb: 'genre'|'tag' / lastfm: 'tag'
  weight     REAL,                      -- lastfm count(0~100). mb 는 NULL
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (artist_uri, source, tag)
);

-- 태그 조회 이력. 이게 없으면 "태그가 0개인 아티스트"와 "아직 안 물어본
-- 아티스트"를 구분할 수 없어 매번 수천 건을 다시 조회하게 된다.
-- 백필의 fetch_status 와 같은 패턴이다.
--
-- external_id 는 MusicBrainz MBID. 한 번 이어 두면 다음부터는 검색 없이
-- 바로 조회할 수 있고, 동일성 근거로도 남는다 (Spotify URL 역참조로 이었을
-- 때는 정밀도 100% 다).
CREATE TABLE artist_tag_fetch (
  artist_uri  TEXT    NOT NULL REFERENCES artist(uri),
  source      TEXT    NOT NULL,
  status      TEXT    NOT NULL,         -- 'ok'|'not_found'|'empty'|'error'
  external_id TEXT,
  http_status INTEGER,
  attempts    INTEGER NOT NULL DEFAULT 0,
  fetched_at  INTEGER NOT NULL,
  PRIMARY KEY (artist_uri, source)
);

-- 0층: 세션 하나의 가중치.
--
-- 왜 뷰가 아니라 테이블인가: 계산이 눈에 보여야 디버깅이 된다. "이 아티스트가
-- 왜 상위인가"를 세션 단위로 되짚을 수 있어야 모델을 믿을 수 있다.
--
-- completion_source 가 규칙 3 을 지키는 장치다. 폴러 실측(measured), export 가
-- 준 ms_played(export), RP 추정(inferred) 을 한 컬럼에 뭉개지 않고 출처를
-- 남겨, 나중에 "실측만으로 다시 계산"이 가능하게 한다.
--
-- mode_score 는 모드별 기저율 대비 상대값이다. 평평한 가산점을 주면 안 되는
-- 것이 2026-08-16 실측으로 드러났다 — 직접 고른 clickrow 의 완주율이 0.40,
-- 흘러온 trackdone 이 0.84 다. 같은 완주라도 clickrow 쪽이 훨씬 강한 증거다.
CREATE TABLE play_weight (
  session_id        INTEGER NOT NULL REFERENCES play_session(id),
  params_version    INTEGER NOT NULL REFERENCES feature_params(version),
  completion        REAL,               -- 0~1 로 클램프 (1 초과 153건 실재)
  completion_source TEXT    NOT NULL,   -- 'measured'|'export'|'inferred'|'none'
  mode              TEXT,               -- reason_start, 또는 라이브 재구성값
  mode_score        REAL,               -- 모드 기저율 대비 상대 완주
  voluntary         REAL,               -- context_origin 기반 계수
  weight_current    REAL    NOT NULL,   -- 반감기 6개월
  weight_core       REAL    NOT NULL,   -- 반감기 24개월
  computed_at       INTEGER NOT NULL,
  PRIMARY KEY (session_id, params_version)
);
CREATE INDEX idx_play_weight_params ON play_weight(params_version);

-- 1·2층: 엔티티 친화도. 아티스트·앨범·장르·트랙을 한 테이블에 담는다.
--
-- horizon 이 두 개인 이유: 취향 감쇠가 단일 지수가 아니다 (2026-08-16 실측).
-- 1~3개월에 급락한 뒤 12~24개월 구간이 평평하다 — 빠르게 도는 회전층과
-- 몇 년 남는 기저층이 겹쳐 있어서, 하나의 반감기로는 둘 다 틀리게 나온다.
--
-- measured_plays 를 따로 세는 이유도 규칙 3 이다. 점수가 추정에 얼마나
-- 기대고 있는지 모르면 그 점수를 얼마나 믿을지 정할 수 없다.
CREATE TABLE entity_affinity (
  entity_type    TEXT    NOT NULL,      -- 'artist'|'track'|'album'|'genre'
  entity_key     TEXT    NOT NULL,      -- uri, 또는 정규화된 장르 문자열
  horizon        TEXT    NOT NULL,      -- 'current'(6개월) | 'core'(24개월)
  params_version INTEGER NOT NULL REFERENCES feature_params(version),
  score          REAL    NOT NULL,
  plays          INTEGER NOT NULL,
  measured_plays INTEGER NOT NULL,
  computed_at    INTEGER NOT NULL,
  PRIMARY KEY (entity_type, entity_key, horizon, params_version)
);
CREATE INDEX idx_affinity_rank ON entity_affinity(entity_type, horizon, params_version, score DESC);

-- 3층: 행동 프로파일. 장르가 전혀 필요 없는 축이라 외부 소스가 죽어도 산다.
--
-- period 는 'all' 또는 'YYYY-MM', 'weekday-morning' 같은 슬라이스 키.
-- source_filter 로 같은 지표를 '실측만' 과 '전체' 두 벌 계산해 둘 수 있다.
--
-- sample_size 를 반드시 함께 적는다. 라이브 실측은 아직 수십 건 규모라,
-- 표본 수 없이 비율만 보면 잡음을 추세로 읽는다.
CREATE TABLE behavior_profile (
  period         TEXT    NOT NULL,
  metric         TEXT    NOT NULL,      -- 'completion_rate'|'skip_rate'|'exploration_rate' …
  source_filter  TEXT    NOT NULL DEFAULT 'all',   -- 'all'|'measured'
  params_version INTEGER NOT NULL REFERENCES feature_params(version),
  value          REAL    NOT NULL,
  sample_size    INTEGER NOT NULL,
  computed_at    INTEGER NOT NULL,
  PRIMARY KEY (period, metric, source_filter, params_version)
);
