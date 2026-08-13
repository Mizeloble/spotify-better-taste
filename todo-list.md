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

### 판정 전에 정해야 할 것

호스트 슬립으로 생긴 공백과 수집기 크래시가 둘 다 `process_restart` 로
기록된다. 지금은 리포트가 사유별 내역을 보여 주고 사람이 로그로 가르는데,
소크 기준(공백 120분)을 그대로 쓸지 슬립을 빼고 볼지 결정이 필요하다.
