#!/bin/zsh
# launchd 에 spotify-taste 운영 잡 9개 등록.
#
# 무엇/왜:
# - ~/Library/LaunchAgents/ 에 plist 복사 후 bootstrap 으로 등록.
# - 등록 후 자동 사이클:
#   * 상시    : collect (KeepAlive) — /me/player 25초, recently-played 10분
#   * 5분마다 : heartbeat-check — 수집이 실제로 되고 있는지 (데이터 기준)
#   * 02:30   : log-rotate
#   * 03:30   : backfill (예산 3,000콜)
#   * 04:10   : snapshot (라이브러리·플레이리스트·top)
#   * 04:40   : classify (--stale)
#   * 05:00   : maintenance (백업·prune, 일요일엔 무결성 점검)
#   * 09:00   : daily-report (문제 있으면 Telegram)
# - bootstrap 은 macOS Catalina 이후 권장 명령. 구식 `launchctl load` 와 다름.
#
# 어떻게 확인:
#   launchctl list | grep mizeloble.spotify-taste   # 라벨 8개
#   tail -f logs/collect-$(date +%F).log

set -euo pipefail

PROJECT_DIR="${0:A:h:h}"
PLIST_SRC="$PROJECT_DIR/scripts/launchd"
PLIST_DST="$HOME/Library/LaunchAgents"
LABELS=(
  "com.mizeloble.spotify-taste.collect"
  "com.mizeloble.spotify-taste.heartbeat-check"
  "com.mizeloble.spotify-taste.backfill"
  "com.mizeloble.spotify-taste.snapshot"
  "com.mizeloble.spotify-taste.classify"
  "com.mizeloble.spotify-taste.features"
  "com.mizeloble.spotify-taste.maintenance"
  "com.mizeloble.spotify-taste.log-rotate"
  "com.mizeloble.spotify-taste.daily-report"
)

mkdir -p "$PLIST_DST" "$PROJECT_DIR/logs"

# 등록 전에 전제 조건을 확인한다. 토큰 없이 등록하면 KeepAlive 가 실패를
# 반복하며 로그만 채운다.
echo "== preflight =="
if ! /opt/homebrew/bin/uv run spotify-taste ops preflight; then
  echo ""
  echo "ERROR: 전제 조건이 충족되지 않아 등록을 중단합니다." >&2
  echo "       위 항목을 해결한 뒤 다시 실행하세요." >&2
  exit 1
fi

chmod +x "$PROJECT_DIR"/scripts/*.sh

# zsh/bash 모두에서 안전한 배열 펼침 (unquoted $LABELS 는 bash 에서 첫
# 원소만 처리한다).
for label in "${LABELS[@]}"; do
  src="$PLIST_SRC/$label.plist"
  dst="$PLIST_DST/$label.plist"

  if [[ ! -f "$src" ]]; then
    echo "ERROR: plist 원본 없음: $src" >&2
    exit 1
  fi

  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    echo "  - 기존 등록 해제: $label"
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  fi

  # plist 는 변수를 못 쓴다. 원본은 __PROJECT_DIR__ 자리표시자로 두고 복사할
  # 때 이 체크아웃의 절대경로로 치환한다 — 어디에 clone 해도 같은 원본이 돈다.
  sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$src" > "$dst"
  if ! plutil -lint -s "$dst"; then
    echo "ERROR: 치환 후 plist 가 유효하지 않음: $dst" >&2
    exit 1
  fi

  # bootout 은 비동기다. 종료가 끝나기 전에 bootstrap 하면 "Input/output
  # error"(5) 로 실패하는데, set -e 때문에 거기서 스크립트가 통째로 죽는다.
  # 2026-08-21 에 실제로 그렇게 됐고, 하필 첫 잡이 collect 라 **수집이 내려간
  # 채로** 나머지 잡은 등록조차 되지 않았다. 잠깐 기다렸다 재시도한다.
  for attempt in 1 2 3; do
    if launchctl bootstrap "gui/$UID" "$dst" 2>/dev/null; then
      echo "  + 등록: $label"
      break
    fi
    if [[ $attempt -eq 3 ]]; then
      echo "ERROR: bootstrap 실패: $label" >&2
      launchctl bootstrap "gui/$UID" "$dst" || true   # 마지막 오류를 보여 준다
      exit 1
    fi
    sleep 2
  done
done

echo ""
echo "완료. 확인:"
echo "  launchctl list | grep mizeloble.spotify-taste"
echo "  tail -f $PROJECT_DIR/logs/collect-\$(date +%F).log"
