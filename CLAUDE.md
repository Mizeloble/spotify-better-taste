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

## API 제약 (2026-08 기준 — 오래된 예제는 대부분 동작하지 않는다)

**사라짐**: `/audio-features`, `/audio-analysis`, `/recommendations`,
`/artists/{id}/related-artists`, `/artists/{id}/top-tracks`, `/browse/*`,
`GET /markets`, **복수 조회 전부**(`GET /tracks`, `/artists`, `/albums` …).
필드 `popularity`, artist `followers`, `available_markets` 도 제거됐다.

**바뀜**: `/playlists/{id}/tracks` → `/playlists/{id}/items` (응답 키도 `items`/`item`),
라이브러리는 `PUT|DELETE /me/library` + `GET /me/library/contains`,
`/search` 의 `limit` 최대값이 50 → **10**.

**살아 있음**: `/me/player`, `/me/player/recently-played`, `/me/top/*`, `/me/tracks`,
`/me/playlists`, `/playlists/{id}`, `/artists/{id}`(**`genres` 생존 — 취향 벡터의
주 재료**), `/artists/{id}/albums`, `/tracks/{id}`, `/albums/{id}`, `/search`,
`POST /me/playlists`, `POST /playlists/{id}/items`.

복수 조회가 없으므로 카탈로그 해석은 1엔티티=1콜이다. 절대 루프에서 그냥 때리지
말고 `metadata/backfill.py` 의 예산제 큐를 쓸 것.

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
