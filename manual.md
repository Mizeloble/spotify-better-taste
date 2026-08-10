# 운영 매뉴얼

## Step 0 — 코드보다 먼저 해야 하는 것

**0.1 과 0.2 는 리드타임이 길다. 오늘 시작할 것.**

### 0.1 Extended Streaming History 신청 (지금 즉시) ⏳ 수일~30일

spotify.com → 계정 → **개인정보 설정** → "확장 스트리밍 기록" 체크 → 요청.
5일짜리 "계정 데이터"도 함께 체크해 두면 부분 데이터를 먼저 받는다.

ZIP 이 도착하면 `data/import/extended-history/` 에 푼다. Phase 1 은 아직 이걸
임포트하지 않지만 `play_session.source` 가 이미 `'history_export'` 를 받으므로
나중에 스키마 변경이 필요 없다.

**이 계획에서 가장 긴 리드타임이고, 다른 무엇도 여기 막혀 있지 않다.**
지금 신청해 두면 나머지를 만드는 동안 시계가 돈다.

### 0.2 Spotify Premium 구독 🚧 하드 블로커

2026-02-11 이후 신규 Client ID 발급은 Premium 계정이 필수이고, Free 에서는
`GET /me/player` 재생상태 조회를 신뢰할 수 없다.

**청취를 모델링하려는 바로 그 계정**으로 Premium Individual 을 구독한다.
(다른 계정으로 만들면 로그가 엉뚱한 사람 것이 된다.)

확인: `GET /me` 의 `"product"` 가 `"premium"` 이어야 한다. `ops preflight` 가
이걸 검사하고 아니면 큰 소리로 실패한다.

*이게 없으면 Phase 1 자체가 성립하지 않는다.*

### 0.3 앱 등록

[developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → Create app

| 항목 | 값 |
|---|---|
| App name | `spotify-taste` |
| Description | `personal listening-log analysis` |
| **Redirect URI** | `http://127.0.0.1:8888/callback` |
| Which API/SDKs | **Web API** 만 |

- **Redirect URI 는 반드시 리터럴 루프백 IP.** `http://localhost:8888/callback`
  은 거부된다 (명시적 루프백 IP 만 HTTPS 요구에서 면제). 포트 와일드카드도 없다.
- 저장할 것은 **Client ID 하나**. 대시보드에 Client Secret 도 보이지만
  **무시한다** — PKCE 는 시크릿을 쓰지 않고, "혹시 몰라서" `.env` 에 넣어 두는
  것이 로그 유출의 시작이다.
- Settings → **User Management** → 본인 추가 (이름 + Spotify 계정 이메일 정확히).
  Development mode 는 5명 한도이고 우리는 1명이면 된다.

### 0.4 Telegram 봇 (신규)

1. [@BotFather](https://t.me/BotFather) → `/newbot` → 이름/username 정하고 토큰 복사
2. 만든 봇에게 아무 메시지나 보낸다
3. `https://api.telegram.org/bot<TOKEN>/getUpdates` 를 열어
   `result[0].message.chat.id` 를 확인

auto-trade 봇과 분리하는 이유: 주식 알림과 섞이지 않고, 나중에 주간 추천
다이제스트도 같은 방으로 보낼 수 있다.

### 0.5 `.env` 작성

```bash
cp .env.example .env
```
`SPOTIFY_CLIENT_ID`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 를 채운다.
나머지는 기본값으로 둔다.

### 0.6 첫 실행

```bash
uv sync
uv run spotify-taste db migrate
uv run spotify-taste ops test-alert      # Telegram 도착 확인
uv run spotify-taste auth login          # 브라우저 승인
uv run spotify-taste auth status         # scope 6/6 확인
uv run spotify-taste collect --once      # 1틱 수집
uv run spotify-taste db stats            # player_sample 이 1행 이상
```

---

## 일상 운영

### 상태 확인

```bash
uv run spotify-taste db stats
```
마지막 샘플 시각이 초록색이면 수집이 살아 있다. 빨간색(60분 초과)이면
`logs/collect-*.log` 를 본다.

### 수집기 수동 실행

```bash
uv run spotify-taste collect
```
Ctrl-C 로 멈추면 정상 종료 표식이 남아 heartbeat 알림이 오지 않는다.

### 판정 규칙을 바꿨을 때

원시 샘플이 영구 보관되므로 과거를 다시 계산할 수 있다. **항상 dry-run 먼저.**

```bash
uv run spotify-taste sessions rebuild --dry-run   # 무엇이 달라지는지 본다
uv run spotify-taste sessions rebuild --write    # 확인 후 적용
```

---

## 장애 대응

| 증상 | 원인 | 조치 |
|---|---|---|
| Telegram 에 "재인증이 필요합니다" | refresh token 폐기 | `uv run spotify-taste auth login` |
| Telegram 에 "토큰 갱신이 계속 실패합니다" | 네트워크/Spotify 장애 | 수집기는 계속 돈다. 30분 뒤 재확인 |
| Telegram 에 "응답 모양이 바뀌었습니다" | **API 필드 변경** | 아래 참조 |
| `db stats` 의 마지막 샘플이 낡음 | 수집기 정지 | `launchctl list \| grep spotify-taste` 로 상태 확인 |

### 응답 모양 변경 알림이 왔을 때

Spotify 가 필드를 지웠거나 추가했다. 무엇이 바뀌었는지 이렇게 본다:

```bash
sqlite3 data/spotify-taste.db "SELECT first_seen_at, key_paths FROM api_schema_shape WHERE endpoint = '/me/player' ORDER BY first_seen_at;"
```

두 `key_paths` 를 비교하면 사라진 키가 보인다. 해당 응답의 원문도 남아 있다:

```bash
sqlite3 data/spotify-taste.db "SELECT body FROM raw_api_response WHERE id = (SELECT sample_raw_id FROM api_schema_shape WHERE shape_hash = '<해시>');"
```

---

## 알아 둘 것

- **액세스 토큰 만료는 정상이다.** `auth status` 가 만료를 빨갛게 보여 줘도
  refresh token 이 살아 있으면 폴러가 알아서 갱신한다.
- **204 응답은 정상이다.** 아무것도 재생하지 않을 때 나오는 정상 상태이고,
  로그에 오류로 찍히지 않는다.
- **Mac 이 자면 주 폴러도 멈춘다.** 그동안 폰으로 들은 것은
  recently-played 가 메꾸지만 `ms_played` 실측이 없어 저신뢰 세션이 된다.
  `caffeinate` 를 쓰지 않는 이유는 청취 로거 때문에 24시간 깨워 두는 것이
  사용자 적대적이기 때문이다.
- **`unknown` 출처가 많은 것은 정상일 수 있다.** null context 를 self 로
  추측하지 않는 설계라 그렇다. 비율이 80% 를 넘으면 `classify report` 를 보고
  `CONTEXT_NULL_ACCOUNT=true` 를 검토한다.
