#!/bin/zsh
# 등록된 spotify-taste 잡을 전부 해제한다.
#
# 수집기를 멈추면 그 구간의 데이터는 영구 소실이다 (소급 수집 불가).
# 잠깐 멈추는 것이면 `launchctl bootout gui/$UID/<label>` 로 개별 처리를
# 권한다.

set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents"
LABELS=(
  "com.mizeloble.spotify-taste.collect"
  "com.mizeloble.spotify-taste.heartbeat-check"
  "com.mizeloble.spotify-taste.backfill"
  "com.mizeloble.spotify-taste.snapshot"
  "com.mizeloble.spotify-taste.classify"
  "com.mizeloble.spotify-taste.maintenance"
  "com.mizeloble.spotify-taste.log-rotate"
  "com.mizeloble.spotify-taste.daily-report"
)

for label in "${LABELS[@]}"; do
  if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$UID/$label" 2>/dev/null || true
    echo "  - 해제: $label"
  fi
  rm -f "$PLIST_DST/$label.plist"
done
echo "완료."
