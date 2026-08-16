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
- [x] 19. launchd 래퍼 8개 + plist + install/uninstall
- [ ] 20. **48시간 소크 테스트** ← 여기를 통과하기 전에 Phase 2 시작 금지

## Phase 2~6

- [x] **Extended Streaming History 임포터** (Phase 2 에서 앞당김) —
  2021-02~2026-08, 39,006건 임포트 완료. `reason_start` 분류 규칙(v3)까지.

계획서 §11 참조. **20번을 통과하기 전에는 시작하지 않는다** — 실제 데이터로
`CONTEXT_NULL_ACCOUNT` 와 폴링 간격을 조정해야 하기 때문이다.

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

**Phase 1 코드 완료, 상시 수집 가동 중.** 테스트 523개 통과, ruff/mypy 클린.

남은 것은 20번 소크 테스트 판정뿐이다.

```bash
uv run spotify-taste ops report --days 2
```

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
