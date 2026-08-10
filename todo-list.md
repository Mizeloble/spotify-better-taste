# 진행 상황

계획 원본: `~/.claude/plans/spotify-taste-peaceful-snowglobe.md`

## Step 0 (사람이 해야 할 일) — **미완료**

- [ ] **Extended Streaming History 신청** ⏳ 수일~30일 — 지금 하면 시계가 돈다
- [ ] **Spotify Premium 구독** 🚧 없으면 Phase 1 자체가 성립하지 않음
- [ ] 개발자 앱 등록 + Redirect URI `http://127.0.0.1:8888/callback`
- [ ] Telegram 봇 생성 (@BotFather)
- [ ] `.env` 작성 후 `auth login`

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

계획서 §11 참조. **20번을 통과하기 전에는 시작하지 않는다** — 실제 데이터로
`CONTEXT_NULL_ACCOUNT` 와 폴링 간격을 조정해야 하기 때문이다.

## 현재 상태

**Phase 1 코드는 완료.** 테스트 458개 통과, ruff/mypy 클린.

남은 것은 Step 0(사람이 해야 할 일)와 20번 소크 테스트뿐이다.
Step 0 를 끝낸 뒤:

```bash
uv run spotify-taste ops preflight        # 전제 조건 일괄 확인
zsh scripts/launchd-install.sh            # 상시 수집 시작 (잡 8개)
```

이틀 뒤 `uv run spotify-taste ops report --days 2` 로 판정한다.
