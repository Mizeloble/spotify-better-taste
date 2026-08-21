# 진행 상황

계획 원본: `~/.claude/plans/spotify-taste-peaceful-snowglobe.md`

## Step 0 (사람이 해야 할 일) — **완료**

- [x] **Extended Streaming History 신청** — 수령·임포트까지 완료(39,006건)
- [x] **Spotify Premium 구독**
- [x] 개발자 앱 등록 + Redirect URI `http://127.0.0.1:8888/callback`
- [x] Telegram 봇 생성 (@BotFather)
- [x] `.env` 작성 후 `auth login`

절차는 [manual.md](manual.md) 참조.

## Phase 1 — 수집 기반

- [x] 1. 리포 골격 (uv, src 레이아웃, Typer, structlog)
- [x] 2. 설정·시간 유틸·주입 가능한 Clock
- [x] 3. DB 계층 + Phase 1~6 전체 스키마 + 마이그레이션 러너
- [x] 4. Telegram 알림 (종류별 쿨다운)
- [x] 5. PKCE + 루프백 콜백 서버
- [x] 6. 토큰 저장소 (원자적 쓰기 + .bak) + TokenProvider + auth CLI
- [x] 7. 적응형 레이트리미터 + API 클라이언트 + 엔드포인트 상수
- [x] 8. 2계층 원본 아카이브 + 응답 모양 카나리
- [x] 9. 순수 세션 상태머신 + 궤적 빌더
- [x] 10. 주 폴러 (백오프 분리, 공백 기록, heartbeat 훅)
- [x] 11. 세션 재구성 (`rebuild == live` 보증) + 재시작 복구
- [x] 12. recently-played 폴러 + 윈도우 병합 + played_at 보정
- [x] 13. 라이브러리/플레이리스트/top 스냅샷
- [x] 14. 출처 분류 결정표 (순수 함수)
- [x] 15. 분류 재계산 드라이버 (playlist_meta TTL 캐시, `classify run --stale/--dry-run`)
- [x] 16. 메타데이터 백필 큐 (재생 횟수 순, `--max-calls` 예산)
- [x] 17. `ops preflight` / `ops heartbeat-check` CLI
- [x] 18. DB 유지보수 (VACUUM INTO 백업, raw prune) + `ops report`
- [x] 19. launchd 래퍼 9개 + plist + install/uninstall (features 04:50 포함)
- [x] 20. **48시간 소크 테스트** — 2026-08-16 통과 (경고 없음, 종료 코드 0).
  판정 윈도에 폴러 사망 사고와 재생 없던 날이 모두 포함된 상태에서 통과했다.

## Phase 2~6

- [x] **Extended Streaming History 임포터** (Phase 2 에서 앞당김) —
  2021-02~2026-08, 39,006건 임포트 완료. `reason_start` 분류 규칙(v3)까지.

계획서 §11 참조.

### 스키마 (마이그레이션 0003, 2026-08-16 적용)

`feature_params`(파라미터 버전 레지스트리), `artist_tag`/`artist_tag_fetch`
(외부 장르 원본과 조회 이력), `play_weight`(0층), `entity_affinity`(1·2층),
`behavior_profile`(3층). 0001 이 만들어 둔 `track_tag`(Phase 3 LLM 태깅)와
`taste_snapshot`(불변 기록)은 그대로 쓴다.

파생 테이블은 전부 `params_version` 을 PK 에 넣는다 — 반감기나 기저율이
바뀌면 점수의 의미가 바뀌므로, 어떤 파라미터로 계산된 값인지 추적되지 않으면
재계산이 불가능해진다.

### 가중치 설계 (2026-08-16 실측 기반)

- **반감기 두 개**: `current` 6개월 / `core` 24개월. 취향 감쇠가 단일 지수가
  아니다 — 1~3개월에 급락(0.457→0.283)한 뒤 12~24개월이 평평하고(0.163→0.101)
  36개월에 무너진다(0.041). 회전층과 기저층이 겹쳐 있다.
- **모드는 가산점이 아니라 기저율 대비 정규화**다. 직접 고른 `clickrow` 의
  평균 완주율이 0.40, 흘러온 `trackdone` 이 0.84 다. 평평한 가산점을 주면
  정반대로 계산된다. `backbtn` 은 0.28 로 최하위 — 되감기는 애정이 아니라
  서성임이었다.
- 완주율은 **1 로 클램프**한다 (1 초과 153건 실재, 최대 4.3배).
- 아티스트 수준이 트랙보다 안정적이다(월간 유사도 0.457 vs 0.317). 벡터는
  트랙이 아니라 아티스트·장르 수준에서 만든다.

주의: 아티스트 반감기 24개월은 **상한**이다. `track_artist` 조인이 필요해
백필된 68.8% 구간(인기 편향)에서만 쟀다. 백필 완료 후 재측정해 조정한다.

### 신호 가용 구간이 시기별로 다르다

`reason_start`·`skipped` 는 **export 전용**이다 (라이브 세션 0건). 반대로
`context_origin` 은 앞으로만 쌓인다. 두 축을 이어 붙이면 2026-08-09 임포트
경계에서 가짜 불연속이 생긴다 — 시계열 특성을 만들 때 반드시 분리할 것.

### 장르 소스 (2026-08-15 결정: MusicBrainz 우선 + Last.fm 보완)

`artist.genres` 가 API 에서 사라져(CLAUDE.md 참조) 취향 벡터의 장르 축을
외부에서 가져와야 한다. 재생 상위 25명으로 실측한 결과:

| 경로 | 커버리지 | 비고 |
|---|---|---|
| ISRC → MusicBrainz recording | 36% | 등록이 성기다. **부적합** |
| MB 아티스트 이름 검색 | 52% | 동명이인 미검증 — 상한값 |
| **MB Spotify URL 역참조** | **44%** | 정밀도 100%. 표기 무관(磯貝一樹 등) |
| Last.fm 태그 (엄격 기준) | 84% | 국가·악기·이름 태그 제외 후 |
| **합집합** | **92%** | |

**MusicBrainz 를 1순위, 빈 곳만 Last.fm 으로 메운다.** MB 는 편집된 데이터라
정확하고 Spotify URL 역참조로 동일성이 보장된다. Last.fm 은 커버리지를 사지만
정확도를 판다 — FLKS 를 `punk rock` 이라 하는데 MB 는 `boom bap, chillwave`
이고 후자가 맞다. 서로 보완적이라는 게 핵심 근거다: MB 만 가진 아티스트 2명
(최다 재생 Sebastian Ågren 포함), Last.fm 만 가진 아티스트 10명.

주의: **Last.fm 태그는 원본 그대로 저장하고 정규화는 별도 계층에서** 한다.
`John Novello`(아티스트 이름), `USA`(국가), `guitar`(악기) 같은 것이 상위
태그로 올라온다. 기준을 나중에 바꿔도 원본이 있으면 재계산할 수 있다.

표본이 재생 상위 25명이라 롱테일은 이보다 낮을 것이 확실하다. 92% 를 전체
커버리지로 기대하지 말 것.

## 현재 상태

**Phase 1 완료, 상시 수집 가동 중. Phase 2 진행 중.** (2026-08-21 기준)
테스트 556개 통과, ruff/mypy 클린. 마이그레이션 0003 적용. launchd 잡 9개.

0층·1층 계산 모듈 완료(`features/`), 매일 04:50 야간 잡으로 돈다.

주의: 지금 계산 가능한 범위는 **재생의 68.8%, 고유 트랙의 25.9%** 다
(백필이 인기순이라). 설계 검증에는 충분하지만 탐색률 같은 롱테일 지표는
백필이 끝날 때까지 믿으면 안 된다.

```bash
uv run spotify-taste ops report --days 2
uv run spotify-taste features top artist --horizon core
```

## 다음에 할 일 (우선순위 순)

### 0. 내일 아침 확인 — 2026-08-21 변경의 첫 실증

```bash
tail -3 logs/backfill-$(date +%F).log logs/features-$(date +%F).log
```

- 백필에 `아티스트 600 · 트랙 0` 이 찍히면 순서 전환 성공. 3~4밤이면
  아티스트 큐가 말라 취향 벡터 상위에 이름이 붙는다.
- `logs/features-*.log` 가 처음 생기면 야간 잡 등록 성공.

### 1. 장르 수집 → `artist_tag` (야간 잡)

테이블(`artist_tag`, `artist_tag_fetch`)은 0003 에 이미 있다. 결정도
끝났다(위 "장르 소스" 절): **MusicBrainz 1순위, 빈 곳만 Last.fm**.

실측으로 알아낸 것들 — 다시 발견하느라 시간 쓰지 말 것:

- **MB 조인은 Spotify URL 역참조로 한다.** 이름 검색은 동명이인을 걸러내지
  못한다(커버리지 52% 는 정밀도 미검증 상한, 실제는 44%).
  `/ws/2/url?resource=https://open.spotify.com/artist/{spotify_id}&inc=artist-rels&fmt=json`
  → `relations[].artist.id` 가 MBID. 표기 무관하게 정확하다
  (Kazuki Isogai → 磯貝一樹, Shingo Sekiguchi → 関口シンゴ).
- 장르는 `/ws/2/artist/{mbid}?fmt=json&inc=genres+tags`.
- **`inc` 의 `+` 는 URL 문자열에 직접 넣어야 한다.** httpx `params=` 로
  넘기면 `%2B` 로 인코딩돼 400 이 난다.
- **`genres` 는 `isrc` 리소스에서 유효하지 않다** (400). ISRC 경로 자체가
  커버리지 36% 라 부적합이니 쓰지 말 것.
- MB 는 익명 **1 rps**, `User-Agent` 에 식별 정보 필수. 가끔 503/타임아웃이
  나므로 재시도 필요.
- Last.fm 키는 `.env` 의 `LASTFM_API_KEY` 에 있다. `artist.gettoptags` 에
  `autocorrect=1`. **`count >= 50` 이상만 쓰고**, 아티스트 이름 자체·국가
  (`japanese`,`USA`)·악기(`guitar`,`saxophone`) 태그는 버린다. 원본은
  `artist_tag` 에 그대로 저장하고 거르는 일은 정규화 계층이 한다.
- 정확도는 MB 가 낫다: FLKS 를 Last.fm 은 `punk rock`, MB 는
  `boom bap, chillwave` 라 하는데 후자가 맞다.

대상은 아티스트 4,591명(2026-08-21 기준, 백필 진행에 따라 늘어난다).
1 rps 면 2~3시간이라 야간 잡으로 나눠 돌려야 하고, `artist_tag_fetch` 의
`status` 로 증분 재개한다.

### 2. 2층 — 장르 벡터

1층 아티스트 친화도를 `artist_tag` 로 전파해 `entity_affinity` 의
`entity_type='genre'` 로 적재한다. 집계 SQL 자리는 `db/queries.py` 의
`AGGREGATE_*_AFFINITY` 옆이고, `build.py` 의 `_aggregate()` 를 그대로 쓴다.
결측 아티스트는 정규화에서 **제외**한다(0 으로 세지 말 것).

### 3. 3층 — 행동 프로파일 → `behavior_profile`

장르가 필요 없는 축이라 외부 소스와 무관하게 지금도 만들 수 있다.
완주율 분포, 스킵률, 반복 청취율, 탐색률, 자발 재생 비율, 시간대·요일 패턴.
`source_filter` 로 '실측만'과 '전체'를 두 벌 계산해 둘 것.

**탐색률은 백필 완료 전까지 믿지 말 것** — 롱테일 트랙에 `duration_ms` 가
없어 체계적으로 과소 측정된다.

### 4. 백필 완료 후 — 반감기 재측정

`core` 반감기 24개월은 **상한**이다. `track_artist` 조인이 필요해 백필된
구간(인기 편향)에서만 쟀다. 백필이 끝나면 다시 재고 `features params --new`
로 새 버전을 만든다. 측정 방법: 월별 아티스트 벡터의 코사인 유사도가
간격에 따라 어떻게 감쇠하는지 본다(단일 지수로 적합되지 않으니 1~3개월
급락 구간과 12~24개월 평탄 구간을 따로 볼 것).

### 5. 남은 한계 — `launchd-install.sh` 의 잡 순서

bootstrap 재시도는 넣었지만(2026-08-21), `collect` 가 `LABELS` 첫 번째라
실패 시 피해가 가장 크다는 구조는 그대로다. collect 를 마지막으로 옮기거나,
실패해도 나머지를 계속 등록하고 끝에 요약하는 방식이 더 견고하다.

### 소크 중 발견해 고친 것

- **2026-08-13 새벽 폴러 사망** — 백필과 겹친 `database is locked` 로 수집
  프로세스가 통째로 종료됐다. 원인은 DEFERRED 트랜잭션의 읽기→쓰기 업그레이드가
  `busy_timeout` 을 무시하는 것. `tx()` 를 `BEGIN IMMEDIATE` 로 바꾸고,
  폴러 루프가 `OperationalError` 에 죽지 않게 했다 (b105a2e).
- 리포트 오탐 두 건 — 재생 없는 날의 "세션 0" 경고, 미분류를 unknown 으로
  세던 것. 각각 참고로 강등하고 지표를 분리했다 (5ab631a, 4527d23).

### 슬립 공백 취급 (2026-08-14 결정: 판정에서 제외)

폴러가 대기 중 벽시계 점프(5분 초과)를 감지해 `host_sleep` 사유로 공백을
기록하고, 리포트의 공백 경고는 host_sleep 을 뺀 나머지로만 판정한다.
표시(`수집 공백` 합계)에는 그대로 남는다 — recently-played 조정 범위이므로.

한계: 슬립 중에 프로세스가 죽으면 여전히 `process_restart` 로 남는다.
그 경우는 경고의 사유별 내역과 `logs/collect-*.log` 로 사람이 가른다.
