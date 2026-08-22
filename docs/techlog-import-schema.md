# 기술 로그 임포트 스키마 설계 (마이그레이션 0004 초안)

무엇/왜
: Spotify 가 개인정보 페이지로 내주는 **Technical Log Information** 을 임포트하기
  위한 스키마 설계다. 재생 사실을 늘리려는 것이 아니다 — 그건 Extended
  Streaming History 가 이미 39,006건으로 채워 놨다. 이 export 가 유일하게
  메꾸는 구멍은 **컨텍스트**, **곡 길이**, **알고리즘 노출** 세 가지다.

설계 원칙
: 아래 §2. 요약하면 — 로그 1줄은 1행으로 원형 보존하고, 해석은 전부 별도
  테이블에 파생으로 남긴다. 사실 테이블(`play_session`)은 거의 건드리지 않는다.

어떻게 확인
: §7 의 실측 결과. 재임포트에서 그 숫자가 안 나오면 매칭 규칙이 틀어진 것이다.

    uv run spotify-taste import techlog --dir "~/Downloads/Spotify Technical Log Information"
    uv run pytest tests/test_import_techlog.py tests/test_attribution.py

내려받은 원본: `~/Downloads/Spotify Technical Log Information` (151개 파일,
2026-02-17 ~ 2026-08-16), `~/Downloads/Spotify Account Data` (2026-08-18 생성).

---

## 1. 이 export 가 실제로 메꾸는 것 (2026-08-21 실측)

DB 현황을 먼저 재고 설계했다. 전부 `data/spotify-taste.db` 를 읽기 전용으로
조회한 값이다.

| 사실 | 값 |
|---|---|
| `history_export` 세션 | 39,006건 (2021-02-10 ~ 2026-08-09) |
| 그중 `context_uri` 가 있는 행 | **0** |
| `v_clean_session` 의 `unknown` 출처 | 22,184건 (57%) |
| 기술 로그 윈도(2026-02-17~08-16) 내 세션 | 4,253건, 그중 컨텍스트 보유 101건 |
| `track` 행 / `duration_ms` 보유 | 11,089 / 5,865 (52.9%) |

기술 로그가 여기에 더하는 것:

1. **컨텍스트.** `ShuffleSequenceEvent`(819건)에 `context_uri` 와 셔플된 트랙
   순서가 통째로 들어 있다. "재생 직전 6시간 내 시퀀스에 그 트랙이 있으면 그
   컨텍스트" 규칙으로 붙여 보니 **35.0%(1,451/4,146)** 가 귀속됐다 (정확도
   84.5%, 근거는 §4.1 — n=58 의 약한 대조 하나뿐이다). 상위 셋은
   `37i9dQZEVXcPpWDnhG5L1X` 777건, `spotify:user:<me>:collection`(좋아요) 330건,
   `37i9dQZF1F5p3rmiWPIYgZ` 281건. 즉 지금 `unknown` 에 뭉쳐 있는 6개월치의
   3분의 1이 **self / algorithmic 로 갈린다** (실제 결과는 §7).
2. **곡 길이.** `BoomboxPlaybackSession.message_ms_total_est` 가 재생의 99.7%
   (4,138/4,146)에 붙는다 — 시차 중앙값 45ms, p95 112ms. 이름은 `_est` 지만
   **추정이 아니다** —
   이미 백필된 1,269곡과 대조한 결과 오차 중앙값 0ms, p90 0ms, 1,266곡이 1초
   이내였다 (관측이 갈리는 곡은 조인 허용치를 1초로 좁힌 뒤 1개로 줄었다).
   백필이 아직 못 간 **692곡**의 길이를 API 호출 0회로 얻는다
   (현재 미완 5,224곡, 야간 예산 600콜 기준으로 1주치 이상).
3. **알고리즘 노출.** `DaylistGenerated`(seed/anti-seed 포함),
   `OnRepeatContents`, `ReleaseRadarServedRecs`, `ContinuumSectionResponseV1`.
   "무엇을 들었나"가 아니라 **"무엇을 밀어 줬나"** 라 Extended History 에는
   원리적으로 없는 축이다.

여기에 없는 것도 분명히 해 둔다. `RawCoreStream` 의 재생 사실 자체는 새롭지
않다 — 4,146행 중 **4,063행이 이미 `history_export` 로 DB 에 있다.** 새로
들어올 사실 행은 46건뿐이고, 그중 30건이 2026-08-11~16 (export 마감 이후 ·
폴러 가동 초기 공백)이다.

---

## 2. 설계 원칙

1. **파일은 DB 밖에 둔다.** 151개 파일 180MB 를 `raw_api_response` 에 밀어
   넣지 않는다. 대신 `import_batch` 에 경로·파일별 sha256·행 수를 남겨
   **어떤 원본으로 무엇이 들어왔는지**를 재현 가능하게 한다. export 디렉터리
   자체가 불변 아카이브다 (Spotify 는 6개월 롤링 윈도라 다시 주지 않는다 —
   원본 백업이 전제).
2. **로그 1줄 = 1행.** `techlog_stream` 은 `RawCoreStream` 의 필드를 해석 없이
   그대로 받는다. 판정은 전부 그 위에서 다시 계산된다 (규칙 1·2).
3. **귀속은 파생이다.** 셔플 시퀀스에서 유추한 컨텍스트를
   `play_session.context_uri` 에 쓰지 않는다. 그 컬럼은 `/me/player` 가 실제로
   관측한 값 자리다. 유추는 `session_context_evidence` 에 `rules_version` 과
   함께 따로 쌓고, 규칙이 바뀌면 통째로 다시 계산한다 (규칙 3의 정신).
4. **사실 행을 늘리지 않는다.** 4,063건은 기존 `history_export` 행에 **붙인다**
   (`techlog_stream.session_id`). 중복 세션을 만들면 모든 집계가 2배로 샌다.
5. **곡 길이는 카탈로그에 섞지 않는다.** `track.duration_ms` 는 API 관측
   전용으로 남기고, 로그에서 얻은 길이는 `track_duration_hint` 에 출처와 함께
   둔다. 소비하는 쪽이 `coalesce` 로 고르되 `completion_source` 에 흔적을
   남긴다 (`features/CLAUDE.md` 규약).

---

## 3. 무엇을 읽고 무엇을 버리나

임포터는 **화이트리스트**로 동작한다. 읽을 파일 목록이 코드에 박혀 있고,
나머지 140여 개 파일은 존재해도 열지 않는다. 프라이버시 때문이다 —
`UserAuthenticationSuccess.json`·`ApAuthenticationSuccess.json` 에는 평문 IP
주소가, `UserAttributes.json`·`Identifiers.json` 에는 이메일·전화번호·생년월일·
우편번호가 들어 있다. **이 export 디렉터리는 리포 안으로 복사하지 않는다.**

읽는 파일:

| 파일 | 행 | 들어가는 곳 |
|---|---|---|
| `RawCoreStream.json` | 4,187 | `techlog_stream` |
| `BoomboxPlaybackSession.json` | 4,662 | `techlog_stream` (시각 조인) |
| `ShuffleSequenceEvent.json` | 819 | `techlog_context_event` + `_item` |
| `TrackNotPlayed.json` | 379 | `techlog_context_event` |
| `DaylistGenerated.json` | 10 | `algo_exposure` + `_item` |
| `OnRepeatContents.json` | 56 | 〃 |
| `ReleaseRadarServedRecs.json` | 134 | 〃 |
| `ContinuumSectionResponseV1.json` | 4,878 | 〃 |

읽지 않는 파일과 이유:

- `Spotify Account Data/StreamingHistory_music_0.json` — 1년치·이름만·분 단위
  절삭. Extended History 가 URI 까지 갖춘 상위집합이다. **임포트 안 한다.**
  (이 파일 때문에 `(artist, track) → uri` 사전을 만들 생각이었으나, Extended
  가 이미 있으므로 불필요해졌다.)
- `Wrapped2025.json` — `topGenres` 가 `spotify:concept:…` URI 5개뿐이고 공개
  API 로 이름 해석이 안 된다. 장르 소스 결정(MusicBrainz+Last.fm)을 바꿀
  재료가 못 된다.
- `YourSoundCapsule.json` / `Marquee.json` / `Inferences.json` — 집계·광고
  세그먼트. 취향 신호가 아니다.
- `YourLibrary.json` — 트랙 433·아티스트 27의 **현재** 스냅샷. `library_track`
  이 이미 관측 중이라 사실이 겹치고, 시각 정보가 없어 과거로 소급도 안 된다.
  다만 분류기의 "라이브러리 보유" 규칙을 대조 검증할 때 한 번 쓸 값은 있어서,
  임포트 대상은 아니되 §8 에 확인 항목으로 남긴다.
- `AdvanceStuck.json` — `source_start` 가 `playlist`(258건)처럼 컨텍스트의
  *종류*만 알려 주고 `context_uri` 는 없다. 어느 플레이리스트인지 특정할 수
  없어 귀속에 못 쓴다. 넣으면 증거 테이블만 328행 불린다. **제외.**
- 인증·네트워크·캐시·오디오 라우트 계열 130여 개 — 이번 범위 밖.
  (`CarDetectionEvent`·`AudioRouteSegmentEnd` 는 "운전 중 청취" 축으로 언젠가
  쓸 수 있지만, 그건 `behavior_profile` 확장 과제다.)

---

## 4. 매칭 규칙 (여기가 틀리면 전부 틀어진다)

`RawCoreStream.timestamp_utc` 는 재생이 **끝난** 시각이다. 계정 데이터의
`StreamingHistory` 와 같은 분·ms 오차 3초 이내로 4,095행 전부가 1:1 대응하는
것을 확인했다. 그러니 `started_at = ended_at - ms_played` — 기존 임포터와 같은
역산이다.

**매칭 조건은 상대 행의 출처마다 다르다.** 하나의 규칙으로 통일하면 안 된다.

```
공통      : track_uri 일치 AND |Δstarted_at| <= 90,000ms
            (sessions.merge.MATCH_WINDOW_MS — 이 리포가 이미 쓰는 상수)
history_export / techlog 행에 붙일 때만 추가:  |Δms_played| <= 2,000ms
poller / recently_played 는 시각 근접만 본다.
```

- **`started_at` 이 출처마다 다른 이유로 어긋난다** (2026-08-21 실측):

  | 출처 | 중앙값 | p95 | 최대 | 왜 |
  |---|---|---|---|---|
  | history_export | 492ms | 947ms | 4.6s | `ts` 가 초 단위로 절삭돼 있다 |
  | poller | 12.6s | 57.8s | 78.1s | 25초 폴링 샘플에서 역산한다 |
  | recently_played | 57.4s | 86.4s | 86.4s | `played_at` 이 30초 반올림이다 |

  **초안의 ±2초 창은 export 에만 맞는 값이었다.** 그대로 구현했더니 폴러 26건 +
  RP 11건이 "일치 없음"으로 떨어져 신규 삽입이 46 → 66으로 늘었다. 이미 있는
  재생을 새 세션으로 또 넣는 것 — 규칙 4가 막으려던 사고다. 90초는 기존
  export 임포터의 `SELECT_POLLER_MATCH` 가 쓰는 것과 같은 창이다.
- **`ms_played` 조건이 export 에만 유효한 이유**: export 의 `ms_played` 는
  기술 로그와 같은 숫자다(Spotify 가 기록한 동일 값). 반면 폴러는 **독립적으로**
  잰다 — 26건을 대조하니 최대 18초까지 벌어졌다(144,000 vs 125,757 등). RP 는
  아예 NULL 이다.
- **그 대신 export 에는 ms 조건이 반드시 필요하다.** 90초 창 안에 같은 곡의
  행이 둘 이상 있는 반복 재생 구간에서 시각만으로 고르면 엉뚱한 짝이 생긴다
  (`ms_played` 0 ↔ 156,720 같은 조합이 3건). 90초 창 + ms 동일성이라는 조합은
  안전하다 — 같은 곡의 두 재생이 90초 안에 있으면서 길이까지 같을 수는 없다.
- **`content_uri` 로 매칭한다.** `displayed_content_uri` 가 다른 행이 185건
  있지만(관계 연결), DB 는 185건 전부를 `content_uri` 쪽으로 갖고 있다.
  `displayed` 는 기록만 하고 매칭에 쓰지 않는다.

우선순위 사다리 — 이미 그 재생을 설명하는 행이 있으면 그쪽이 이긴다:

```
poller  >  history_export  >  techlog  >  recently_played
```

- `poller`·`history_export` 행에는 **붙이기만** 한다 (`session_id` 연결).
  `ms_played` 를 덮어쓰지 않는다. 폴러 값이 이 리포의 기준이다.
- `recently_played` 행에 붙는 경우(11건)는 `ms_played` 가 NULL 이므로
  **techlog 행을 새로 넣고 RP 를 `superseded_by` 로 표시**한다. 기존 export
  임포터가 RP 를 대체하는 것과 같은 논리다.
- 아무것도 없으면(46건) `source='techlog'` 로 새로 넣는다.

멱등성은 `techlog_stream` 의 `UNIQUE(track_uri, ended_at)` 과 `import_batch` 의
파일 sha256 이 함께 보장한다. **다른 테이블은 `event_key` 로 보장한다** — §5의
주석 참조. SQLite 는 UNIQUE 안의 NULL 을 서로 다른 값으로 취급하므로, NULL 이
들어갈 수 있는 컬럼을 UNIQUE 에 넣으면 재임포트가 행을 두 배로 만든다
(`TrackNotPlayed` 379행은 `sequence_uuid` 가 전부 NULL, `DaylistGenerated` 는
`context_uri` 필드 자체가 없다).

### 4.1 컨텍스트 귀속 규칙

셔플 시퀀스로 컨텍스트를 유추할 때:

```
재생 시각 이전의 시퀀스 이벤트를 최신순으로 최대 8개까지 훑어,
그 트랙을 담고 있는 첫 시퀀스의 context_uri 를 채택한다 (창 6시간).
```

**정확도는 84.5% 다** (2026-08-21, `TrackNotPlayed.play_context` 를 정답지로
쓴 대조에서 49일치/9불일치, n=58). 폴러가 관측한 컨텍스트와의 대조는 겹치는
구간이 5일뿐이라 표본이 3건에 그쳤다(3건 모두 일치) — **정확도의 근거는 사실상
n=58 하나뿐이고, 이 숫자를 과신하면 안 된다.**

`confidence` 는 다음으로 정한다 (창별 실측 분포에 맞춘 값이다):

| 등급 | 조건 | 건수 |
|---|---|---|
| high | lag ≤ 10분 **AND** 재생의 `shuffle=true` | ~500 |
| medium | lag ≤ 1시간 | ~1,311 누적 |
| low | lag ≤ 6시간, 또는 `shuffle=false` | ~1,451 누적 |

`shuffle=false` 인 재생 223건이 셔플 시퀀스로 귀속된다. 시퀀스는 셔플이 켜진
세션에서만 발행되므로, 셔플이 꺼진 재생에 붙은 증거는 논리적으로 낡았을 수
있다. 버리지는 않되 `low` 로 내린다. lag 중앙값은 795초, p90 은 3,576초다.

### 4.2 컨텍스트 URI → `context_type` 정규화

**이 표가 없으면 귀속이 아무 효과도 없다.** `classify()` 의 결정표는
`context_type` 으로 분기하는데, 셔플 로그는 `context_uri` 만 준다. 타입을
못 채우면 전부 마지막 줄의 `unhandled_context_type` 으로 떨어져 `unknown` 이
된다 — 그러면 지금 `self` 인 행 349건이 오히려 `unknown` 으로 **퇴행한다**
(실측 확인).

| 원본 URI | `context_type` | 건수 |
|---|---|---|
| `spotify:playlist:…` | `playlist` | 1,067 |
| `spotify:user:<me>:collection` | `collection` | 330 |
| `spotify:user:<me>:collection:artist:<id>` | `collection` | 1 |
| `spotify:list:popular-release-segments-…:artist_<id>` | `artist` | 15 |
| `spotify:album:…` / `spotify:artist:…` | 그대로 | 2 / 18 |
| `spotify:track:…` (단일 곡 재생) | `single_track` | 18 |

`single_track` 은 결정표에 없는 새 타입이다. 컨텍스트 없이 그 곡 하나를 튼
것이므로 `self` 가 맞다 — 결정표에 분기를 추가한다
(`Origin.SELF, "single_track_context"`).

## 5. 스키마

`migrations/0004_techlog_import.sql` 로 적용됐다 (user_version 4). 아래는 그
파일의 내용이다.

```sql
-- ===========================================================================
-- 배치 증적
-- ===========================================================================

-- 어떤 export 파일에서 무엇이 들어왔나. 파일 자체는 DB 밖에 있으므로,
-- 재현성은 이 행이 진다. sha256 이 같으면 같은 원본이다.
CREATE TABLE import_batch (
  id           INTEGER PRIMARY KEY,
  kind         TEXT    NOT NULL,   -- 'techlog' | 'extended_history'
  source_path  TEXT    NOT NULL,
  window_start INTEGER,            -- 로그가 실제로 덮는 구간
  window_end   INTEGER,
  files_json   TEXT    NOT NULL,   -- [{name, sha256, bytes, rows}]
  stats_json   TEXT,               -- 임포터가 낸 통계 그대로
  imported_at  INTEGER NOT NULL
);

-- ===========================================================================
-- 재생 로그 — RawCoreStream 1줄 = 1행
-- ===========================================================================

-- play_session 에 자리가 없는 필드를 원형 그대로 보관한다. 해석 금지.
-- session_id 는 §4 의 매칭 결과이고, NULL 이면 아직 못 붙였다는 뜻이다.
CREATE TABLE techlog_stream (
  id                  INTEGER PRIMARY KEY,
  batch_id            INTEGER NOT NULL REFERENCES import_batch(id),
  ended_at            INTEGER NOT NULL,   -- timestamp_utc (재생 종료)
  track_uri           TEXT    NOT NULL,
  ms_played           INTEGER NOT NULL,
  reason_start        TEXT,
  reason_end          TEXT,
  shuffle             INTEGER,
  media_type          TEXT,               -- audio|video|unknown
  is_downloaded       INTEGER,
  social_listening    INTEGER,
  displayed_track_uri TEXT,               -- 관계 연결(relinking) 흔적
  audio_id            TEXT,

  device_type         TEXT,               -- mobile|desktop
  device_model        TEXT,
  os_name             TEXT,
  os_version          TEXT,
  app_version         TEXT,
  conn_country        TEXT,               -- 여행 구간이 여기서 드러난다 (SG 등)

  -- Boombox 시각 조인 결과. gap_ms 를 남겨야 "왜 이 값을 믿나"에 답할 수 있다.
  playback_id         TEXT,
  est_total_ms        INTEGER,            -- 곡 길이. 검증 결과 실측과 동일
  n_seekback          INTEGER,
  ms_seekback         INTEGER,
  n_seekfwd           INTEGER,
  ms_seekfwd          INTEGER,
  n_stutter           INTEGER,
  -- 실측 시차는 중앙값 45ms · p95 112ms 다. 5초씩 열어 두면 5초 내 후보가
  -- 둘 이상인 288건에서 오결합이 난다. 1,000ms 를 넘으면 est_total_ms 를
  -- NULL 로 둔다. 추가 방어: ms_played > est_total_ms + 2,000 이면 오결합이다
  -- (5초 창에서 8건 발생).
  boombox_gap_ms      INTEGER,            -- 1,000 초과면 길이를 안 쓴다

  session_id          INTEGER REFERENCES play_session(id),
  UNIQUE (track_uri, ended_at)
);
CREATE INDEX idx_tls_session ON techlog_stream(session_id);
CREATE INDEX idx_tls_ended   ON techlog_stream(ended_at);

-- ===========================================================================
-- 컨텍스트 증거 — 원본
-- ===========================================================================

-- 서로 다른 세 로그를 하나의 "컨텍스트가 관측된 순간" 스트림으로 정규화한다.
-- 스키마는 다르지만 답하는 질문이 같다: 이 시각에 어떤 컨텍스트가 살아 있었나.
CREATE TABLE techlog_context_event (
  id             INTEGER PRIMARY KEY,
  batch_id       INTEGER NOT NULL REFERENCES import_batch(id),
  emitted_at     INTEGER NOT NULL,
  kind           TEXT    NOT NULL,  -- 'shuffle_sequence'|'track_not_played'
                                    -- |'advance_stuck'
  context_uri    TEXT,
  source_start   TEXT,              -- playlist|home|artist|driving-jumpstart …
  trigger        TEXT,              -- context_update|play|toggled_shuffle_on
  algorithm      TEXT,              -- FRESH
  context_length INTEGER,           -- 셔플 대상 컨텍스트의 곡 수
  track_uri      TEXT,              -- 단건 이벤트일 때만
  sequence_uuid  TEXT,
  -- 멱등성 키. NULL 이 섞인 컬럼 조합을 UNIQUE 로 쓰면 안 된다 — SQLite 는
  -- UNIQUE 안의 NULL 을 서로 다른 값으로 보므로 재임포트가 행을 두 배로
  -- 만든다 (TrackNotPlayed 는 sequence_uuid 가 전부 NULL 이다).
  -- 원본 JSON 객체를 키 정렬해 직렬화한 sha256 을 넣는다.
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

-- 재생 하나에 컨텍스트를 붙인 결과. play_session 에 쓰지 않는 이유는 §2-3.
-- rules_version 이 PK 에 있어야 규칙 개정 전후를 나란히 놓고 비교할 수 있다
-- (entity_affinity 의 params_version 과 같은 장치).
CREATE TABLE session_context_evidence (
  session_id    INTEGER NOT NULL REFERENCES play_session(id),
  rules_version INTEGER NOT NULL,
  context_uri   TEXT,
  context_type  TEXT,              -- playlist|album|artist|collection|…
  method        TEXT    NOT NULL,  -- 'shuffle_sequence'|'track_not_played'
                                   -- |'advance_stuck'|'none'
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
-- 담는다. surface 별로 테이블을 쪼개면 "노출 대비 재생률" 을 물을 때마다
-- UNION 을 쓰게 된다.
CREATE TABLE algo_exposure (
  id          INTEGER PRIMARY KEY,
  batch_id    INTEGER NOT NULL REFERENCES import_batch(id),
  observed_at INTEGER NOT NULL,
  surface     TEXT    NOT NULL,   -- 'daylist'|'on_repeat'|'release_radar'
                                  -- |'continuum'
  context_uri TEXT,               -- 플레이리스트/믹스/섹션 uri
  title       TEXT,
  position    INTEGER,            -- 피드 안에서의 순위
  meta_json   TEXT,               -- daylist: seed/anti_seed/daypart/concepts
  -- 위와 같은 이유로 내용 해시를 쓴다. (surface, observed_at, context_uri) 를
  -- UNIQUE 로 걸면 Continuum 15행 + ReleaseRadar 6행이 조용히 사라지고,
  -- context_uri 가 없는 daylist 는 재임포트마다 중복된다.
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
-- verified_delta_ms 는 나중에 백필이 그 곡에 닿았을 때 채워지는 사후 검증값이고,
-- 이 컬럼이 있어야 "힌트를 계속 믿어도 되나"에 데이터로 답할 수 있다.
CREATE TABLE track_duration_hint (
  track_uri         TEXT PRIMARY KEY,
  duration_ms       INTEGER NOT NULL,
  source            TEXT    NOT NULL,   -- 'techlog_boombox'
  observations      INTEGER NOT NULL,   -- 같은 곡을 몇 번 관측했나
  -- 값이 갈리는 곡이 실제로 3개 있었다 (최대 155초 차이 — 다른 버전이거나
  -- 오결합이다). disagreements > 0 이면 힌트를 **채택하지 않는다.**
  -- 1,961곡 중 3곡을 버리는 대가로 조용한 오염을 막는다.
  disagreements     INTEGER NOT NULL,
  verified_delta_ms INTEGER,
  created_at        INTEGER NOT NULL
);
```

`play_session` 에는 컬럼을 추가하지 않는다. 필요한 확장은 값 하나뿐이다 —
`source` 에 `'techlog'`, `timing_confidence` 는 `'high'`(ms 정밀).

---

## 6. 코드 쪽 파급 (구현 완료)

스키마만으로는 끝나지 않는 것들.

- **`importer/techlog.py`** 신설. 기존 `extended_history.py` 와 같은 규약
  (`write=False` 가 기본, `ImportStats.lines()` 로 보고). CLI 는
  `spotify-taste import techlog <dir>`.
- **`classify/rules.py`** — 세 가지가 함께 바뀐다. 결정표가 바뀌므로
  **`RULES_VERSION` 3 → 4**, 전 행 stale.
  1. `Evidence` 에 `context_source` 추가 (`'observed'` | `'shuffle_sequence'` |
     `'track_not_played'`). 유추한 컨텍스트로 내린 판정이
     `context_origin_reason` 에서 관측 기반 판정과 구분돼야 한다.
  2. `single_track` 분기 추가 (`Origin.SELF, "single_track_context"`).
     컨텍스트가 곡 자신인 재생 18건이 여기 온다.
  3. **하향 금지 규칙.** 유추 증거는 이미 내려진 긍정 판정을 `unknown` 으로
     되돌릴 수 없다. §4.2 정규화가 빠지면 `self` 349건이 `unknown` 으로
     퇴행하는데, 그 사고를 규칙 층에서도 한 번 더 막는다.
- **`db/queries.py`** — `SELECT_SESSIONS_FOR_CLASSIFY` 가
  `session_context_evidence` 를 LEFT JOIN 해야 한다. 인라인 SQL 금지 규칙대로
  이름 붙은 상수로.
- **`features/build.py`** — 완주도 계산에서
  `coalesce(track.duration_ms, track_duration_hint.duration_ms)`.
  `play_weight.completion_source` 에 값 `'techlog'` 추가
  (`'measured'|'export'|'techlog'|'inferred'|'none'`).
- **`calibration`** — 힌트 검증 결과를 `techlog_duration_delta_ms` 키로 기록.
  지금 표본(1,269곡)에서 중앙값 0ms 지만, 백필이 진행되며 자동으로 갱신되게
  두면 가정이 썩는 걸 잡을 수 있다.
- **분류 순서 주의.** 임포트 → `classify run --stale` 순서를 지켜야 한다.
  거꾸로 하면 귀속이 반영되지 않은 채 `rules_version=4` 가 찍혀 stale 탐지가
  그 행들을 건너뛴다.

---

## 7. 실측 결과 (2026-08-21, 구현 후 스냅샷에 실행)

라이브 DB 의 일관 스냅샷을 떠서 실제로 임포트하고 `classify run` 까지 돌린
결과다. 아래 숫자가 회귀 기준선이다.

```
techlog_stream                       4,146행 (audio·track 만, 음악 아님 41 제외)
  기존 세션에 붙음                   4,086 = history_export 4,060 + poller 26
  신규 세션 (source='techlog')       43   (그중 11건이 RP 를 대체)
  0ms 큐 아티팩트                    16   (원본은 남기고 세션은 안 만든다)
  est_total_ms 보유                  4,112  (Boombox 조인 기각 8)
techlog_context_event                1,198  (셔플 819 + TrackNotPlayed 379)
techlog_context_item                 29,485
session_context_evidence             1,447  (귀속 1,451 중 세션 없는 4건 제외)
  high 523 / medium 621 / low 307    타입 미상 1 (spotify:search:)
track_duration_hint                  1,958  (관측 불일치 1)
  백필이 아직 모르는 곡              692    ← API 호출 0회로 얻은 길이
algo_exposure                        5,071  (내용 동일한 7행은 event_key 로 병합)
algo_exposure_item                   36,753
```

**분류 결과의 변화가 진짜 성과 지표다.**

```
unknown  22,184 (56.5%)  →  21,310 (54.2%)
self     16,911          →  17,062
algorithmic  133         →     914

전이:  unknown → algorithmic   692
       unknown → self          199
       self    → algorithmic    50   ← 오염 발견
       (미분류) → algorithmic    50
       (미분류) → unknown        17
       (미분류) → self            2

유추한 컨텍스트로 판정된 행 1,428:
  inferred:spotify_generated_uri_prefix   766
  inferred:liked_songs_context            329
  inferred:playlist_owner_me              280
  inferred:artist_context_assumed          33
  inferred:single_track_context            18
  inferred:album_context                    2
```

### 설계 단계의 추정이 틀렸던 곳

이 절의 초안에는 `self → algorithmic` 이 **325건**, 그 구간 `self` 의 20.5%
라고 적혀 있었다. **실제로는 50건이다.** 차이의 원인은 초안의 어림 모델이
`37i9dQZ` 접두사를 전부 알고리즘으로 봤다는 데 있다. 실제 결정표는
`playlist_meta` 를 먼저 보고 소유권으로 판정하는데(규칙 2, "소유권이 이름을
이긴다"), `spotify:playlist:37i9dQZF1F5p3rmiWPIYgZ` 는 API 가 **owner = 나,
name = "Liked Songs"** 로 돌려준다. 그 280건은 알고리즘 유도가 아니라 내
좋아요 재생이 맞다. 접두사 규칙은 owner 해석이 실패한
`37i9dQZEVXcPpWDnhG5L1X`(766건, `fetch_status='not_found'`)에만 적용됐다.

기록해 두는 이유: 어림 모델로 성과를 추정하고 그 숫자를 목표로 삼았으면,
구현이 맞는데도 "기대에 못 미친다"고 판단했을 것이다.

### 이 숫자가 안 나오면

- 신규 세션이 43 이 아니라 80을 넘으면 → 출처별 매칭 창을 통일했다
  (§4). 폴러 26 + RP 11 이 중복 삽입되고 있다.
- `history_export` 에 붙은 수가 4,063 이면 → `ms_played` 조건이 빠졌다.
  반복 재생 구간에서 엉뚱한 짝 3건이 붙은 상태다.
- 재임포트했는데 행이 늘면 → `event_key` 를 안 썼다.
- `self → unknown` 이 300건대로 나오면 → §4.2 정규화 표를 안 태웠다.

### 테스트

`tests/test_attribution.py`(26) + `tests/test_import_techlog.py`(22). 전체
602개 통과. 변이 검사로 각 방어선이 실제로 물린다는 것을 확인했다:

| 변이 | 무너지는 테스트 |
|---|---|
| ms 조건을 전 출처에 적용 | poller·RP 관련 3건 |
| ms 조건 제거 | 반복 재생 오매칭 1건 |
| 하향 금지 규칙 제거 | 직접 증거 퇴행 1건 |
| 컨텍스트 타입 정규화 제거 | 귀속·분류 3건 |
| Boombox 허용치 5초로 환원 | 오결합 1건 |

## 8. 남은 확인 항목

- `YourLibrary.json` 의 트랙 433건과 `library_track` 을 대조 — 분류기의
  "라이브러리 보유" 규칙이 실제 라이브러리와 얼마나 어긋나 있나. 임포트가
  아니라 일회성 점검이다.
- 셔플 시퀀스 귀속의 6시간 창은 임의값이다. 창을 1시간으로 좁히면 커버리지가
  35.0% → 31.6% 로 3.4%p 만 줄어든다. `lag_ms` 를 저장해 두고 `confidence`
  등급별 정확도를 잰 뒤 확정한다.
- **귀속 정확도의 근거가 빈약하다.** 정답지로 쓸 수 있는 것이
  `TrackNotPlayed.play_context`(n=58)뿐이고, 폴러 관측과 겹치는 구간은 5일·3건이
  전부다. 폴러가 계속 도는 동안 `context_uri` 관측이 쌓이므로, **한 달 뒤 같은
  대조를 다시 돌려 84.5% 가 유지되는지 확인한다.** 그때까지 이 귀속으로
  Phase 2 학습셋을 확정하지 않는다.
- `self → algorithmic` 325건은 재분류가 아니라 **오염 발견**이다. 뒤집힌 행이
  어떤 아티스트에 몰려 있는지 보면 `entity_affinity` 상위가 얼마나 흔들리는지
  가늠할 수 있다. 임포트 전후로 `entity_affinity` 상위 30을 떠서 비교할 것.
- export 마감(2026-08-09) 이후 폴러가 놓친 30건은 수집 공백을 뜻한다.
  `collector_gap` 과 대조해 원인이 설명되는지 볼 것.
