# spotify-taste

Spotify 청취 로그를 자체 수집·축적하고, **알고리즘 유도 재생과 자발적 재생을 분리해**
취향 모델을 만든 뒤, LLM으로 주간 추천 플레이리스트를 생성하고 그 성과를 Discover
Weekly와 나란히 측정하는 개인용 시스템.

핵심 가설: Spotify 추천이 아쉬운 이유는 (1) 내 자발적 선택과 알고리즘 유도 청취를
구분하지 않고, (2) 컨텍스트(시간대·목적)를 반영하지 않으며, (3) 탐색 폭이 좁기 때문.
이 셋을 직접 통제한다.

## 빠른 시작

운영 준비(Premium 구독, 앱 등록, Extended Streaming History 신청, Telegram 봇)는
[manual.md](manual.md)의 **Step 0** 를 먼저 끝낼 것. 리드타임이 긴 항목이 있다.

```bash
cp .env.example .env      # 값 채우기
uv sync
uv run spotify-taste db migrate
uv run spotify-taste auth login
uv run spotify-taste auth status      # product=premium 확인
uv run spotify-taste collect --once
zsh scripts/launchd-install.sh        # 상시 수집 시작
```

## 설계 원칙

- **수집과 해석을 분리한다.** 폴러는 원시 데이터만 적재하고 해석하지 않는다.
  판정 로직이 바뀌어도 `player_sample` 을 다시 흘려 재계산할 수 있어야 한다
  (`sessions rebuild`, `classify run --stale`).
- **모든 API 응답 원본을 보관한다.** 2024년 이후 필드 제거가 계속되고 있어 스키마
  가정이 언제 깨질지 모른다. 처음 보는 응답 모양이 오면 알림이 나간다.
- **폴러 장애는 조용히 넘어가면 안 된다.** 데이터 공백은 나중에 복구 불가.
  dead-man switch 는 "프로세스가 살아 있는가"가 아니라 "실제로 수집했는가"를 본다.
- **실측을 날조하지 않는다.** recently-played 로 메꾼 세션은 `ms_played` 가 NULL 이고
  `timing_confidence='low'` 다. 폴러가 잰 값과 절대 같은 컬럼에 섞이지 않는다.

## 상태

Phase 1(수집 기반) 구축 중. 진행 상황은 [todo-list.md](todo-list.md) 참조.
