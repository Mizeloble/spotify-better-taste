# spotify-taste — 작업 규약

## 이 프로젝트가 뭔가

Spotify 청취 로그를 자체 수집해 **자발적 재생과 알고리즘 유도 재생을 분리**하고,
그 위에 취향 모델과 LLM 주간 추천을 얹는다. 지금은 Phase 1(수집 기반)을 짓는 중.

## 절대 어길 수 없는 규칙

1. **수집기는 해석하지 않는다.** `collect/` 는 원시 데이터를 적재만 한다.
   세션 판정은 `sessions/machine.py`, 출처 판정은 `classify/rules.py` 가 하고,
   둘 다 저장된 데이터에서 **재계산 가능**해야 한다. 폴러가 `context_origin` 을
   쓰는 코드를 발견하면 그건 버그다.
2. **`sessions/machine.py` 는 순수 함수다.** DB·HTTP·벽시계 금지. 실시간 폴러와
   `sessions rebuild` 가 같은 코드를 타야 하고, 그걸 단언하는 테스트가 있다.
3. **실측과 추정을 같은 컬럼에 섞지 않는다.** `ms_played`/`completion_ratio` 는
   폴러가 잰 값만. recently-played 로 메꾼 세션은 NULL 이고 추정값은
   `inferred_completion_ratio` 에 따로 들어간다.
4. **행을 지우지 않는다.** 중복·팬텀은 `superseded_by` / `suspect_phantom` 으로
   표시한다. 모든 분석 쿼리는 `v_clean_session` 뷰를 쓴다.
5. **Spotify HTTP 는 전부 `spotify/client.py` 를 지난다.** 특히
   `accounts.spotify.com/api/token` 을 `spotify/auth.py` 밖에서 호출하지 말 것 —
   refresh token 회전이 어긋나면 새벽에 수집이 죽는다.
6. **시크릿을 로그에 넣지 않는다.** `_common.mask_secret()` 을 쓰고, httpx 로거는
   `_setup_logging()` 이 WARNING 으로 강등한 상태를 유지한다.

## 수집 운영 방침 (2026-10-18 결정)

**Mac 을 깨워 두지 않는다. 실시간은 recently-played, 완주·스킵은 export.**
68일 운영 중 42%(~29일)가 Mac 슬립·꺼짐으로 폴러 실측 없이 지나갔다.
24시간 가동을 전제로 한 폴러를 잠드는 MacBook 에서 돌린 것이 원인이고,
caffeinate 는 쓰지 않기로 이미 정했다(`scripts/collect.sh`).

- **재생 사실** = recently-played. Mac 이 깨어 있는 짧은 순간(DarkWake 포함)
  마다 따라잡는다. 한 번에 50곡이 한도라 며칠씩 꺼져 있으면 그 이상은 잃는다.
- **완주·스킵** = 매달 다시 요청하는 Extended Streaming History. Spotify 가
  잰 `ms_played`/`reason_end`/`skipped` 라 폴러보다 정확하다. 임포트하면
  겹치는 RP 행이 `superseded_by` 로 대체된다(`import history`, 멱등).
- **폴러는 남긴다.** 깨어 있을 때 잡은 것은 덤이고, 진행 궤적이 있으므로 export
  와 겹치면 폴러가 이긴다.
- 그래서 **슬립은 장애가 아니다.** heartbeat 는 마지막 기상 이후만 세고,
  daily-report 는 슬립으로 설명되는 "폴러 세션 0" 을 경고하지 않는다. 대신
  export 커버리지가 45일보다 낡으면 다시 요청하라고 알린다.
- 분석에서 최근 1~2달은 완주 신호가 비어 있는 게 정상이다. `resolve_completion`
  의 우선순위(폴러 > export > RP 추정)가 export 도착 후 자동으로 메운다.

## API 제약 (2026-08 기준 — 오래된 예제는 대부분 동작하지 않는다)

**사라짐**: `/audio-features`, `/audio-analysis`, `/recommendations`,
`/artists/{id}/related-artists`, `/artists/{id}/top-tracks`, `/browse/*`,
`GET /markets`, **복수 조회 전부**(`GET /tracks`, `/artists`, `/albums` …).
필드 `popularity`, artist `followers`, `available_markets` 도 제거됐다.
**artist `genres` 도 없다** (2026-08-15 확인). `/me/top/artists` 응답의 아티스트
객체에 키 자체가 없고, 백필로 받은 아티스트 140명이 예외 없이 빈 배열이었다.
아티스트에서 얻을 수 있는 것은 이제 식별자·이름·이미지뿐이다.

**바뀜**: `/playlists/{id}/tracks` → `/playlists/{id}/items` (응답 키도 `items`/`item`),
라이브러리는 `PUT|DELETE /me/library` + `GET /me/library/contains`,
`/search` 의 `limit` 최대값이 50 → **10**.

**살아 있음**: `/me/player`, `/me/player/recently-played`, `/me/top/*`, `/me/tracks`,
`/me/playlists`, `/playlists/{id}`, `/artists/{id}`, `/artists/{id}/albums`,
`/tracks/{id}`, `/albums/{id}`, `/search`, `POST /me/playlists`,
`POST /playlists/{id}/items`.

복수 조회가 없으므로 카탈로그 해석은 1엔티티=1콜이다. 절대 루프에서 그냥 때리지
말고 `metadata/backfill.py` 의 예산제 큐를 쓸 것.

**쿼터는 엔드포인트 계열별로 분리돼 있다** (2026-08-15 실측). 카탈로그
(`/artists/{id}`, `/tracks/{id}` …)는 야간 백필이 600콜쯤에서 막히지만,
그동안에도 `/me/*` 는 정상이다 — 04:10 스냅샷이 매일 성공한다. 카탈로그가
막혔다고 수집이 죽은 것이 아니다.

**카탈로그 쿼터는 고정 리셋 시각이 아니라 배치 첫 콜 기준 24시간 롤링이다**
(2026-08-31 재측정, 08-12~08-31 14박 전부 일치). `Retry-After` 는 매번
85,882~86,100초(23.9시간)이고, 해제 시각은 예외 없이 **그날 백필이 시작한
시각 + 24시간**에 떨어진다. 03:30 에만 돌리던 동안에는 "다음 03:30 에 풀린다"
로 보였지만 그건 스케줄이 같아서 생긴 착시다 — 08-31 은 catch-up 으로 23:30
에 돌았고 해제가 09-01 23:30 이라 **09-01 03:30 배치는 통째로 공친다.**
예정 밖 시각에 백필을 수동으로 돌리면 그날 예산을 그 시각으로 옮기는 셈이니,
정규 배치를 한 번 버릴 각오가 없으면 돌리지 말 것.

## 코드 스타일

- 모듈 최상단에 한국어 독스트링: **무엇/왜 → 설계 원칙 → 어떻게 확인**.
- 공유 모듈은 `__all__` 을 명시한다.
- 시간은 **UTC epoch 밀리초 정수**가 단일 표준. KST 변환은 표시 경계에서만.
- structlog 이벤트명은 점 네임스페이스: `poller.session_closed`, `auth.revoked`.
- SQL 은 전부 `db/queries.py` 의 이름 붙은 상수. 인라인 SQL 금지(grep 가능해야 함).
- 로컬 코멘트는 "왜"를 적는다. 사고가 있었으면 날짜와 함께 남긴다.

## 검증

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

통합 테스트(실 API)는 기본 제외. `uv run pytest -m integration` 으로 명시 실행.
