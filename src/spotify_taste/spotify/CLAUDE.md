# spotify/ 규약

- **모든 Spotify HTTP 는 `client.py` 를 지난다.** 다른 모듈에서 `httpx` 로
  api.spotify.com 을 직접 부르지 않는다. 그래야 레이트리밋·재시도·원본
  아카이브가 빠짐없이 걸린다.
- **`accounts.spotify.com/api/token` 은 `auth.py` 만 만진다.** 두 곳에서 갱신하면
  회전된 refresh token 을 서로 덮어써 새벽에 수집이 죽는다. 401 재시도가
  필요하면 `TokenProvider.force_refresh()` 를 쓴다.
- **엔드포인트는 `endpoints.py` 의 템플릿 상수로 넘긴다.** `/tracks/{id}` 를
  그대로 넘기고 `path_params` 로 치환하게 한다 — 그 템플릿이 곧
  `raw_api_response.endpoint` 키라, ID 를 미리 박아 넣으면 "이 엔드포인트의
  응답 모양이 언제 바뀌었나"를 조회할 수 없게 된다.
- **상태 변경 요청은 `idempotent=False`.** `post()` 의 기본값이 그렇다.
  5xx 를 재시도하면 플레이리스트에 곡이 두 번 들어간다. 429 는 예외 —
  '처리되지 않음'을 뜻하므로 재시도가 안전하다.
- **204 를 오류로 다루지 않는다.** `/me/player` 가 아무것도 재생하지 않을 때
  주는 정상 응답이다. `SpotifyResponse.is_no_content` 로 판정한다.
- **`GONE` 클래스의 엔드포인트를 되살리려 하지 말 것.** 오래된 튜토리얼이
  전부 그것들을 쓴다. 대체제는 없고, 이 프로젝트가 LLM 으로 메우려는 대상이다.
- 레이트리미터는 프로세스에 **하나**다. 역할별로 새로 만들지 않는다 —
  Spotify 는 client_id 단위로 제한한다.
