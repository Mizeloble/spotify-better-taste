#!/bin/zsh
# 로그 로테이션. 30일 지난 것은 gzip, 90일 지난 것은 삭제.
#
# launchd-*.log 는 launchd 가 fd 를 잡고 있어 지우면 안 된다 — 10MB 를
# 넘으면 제자리에서 truncate 하고 표식을 남긴다.

set -euo pipefail
LOGS_ROOT="${LOGS_ROOT:-/Users/jjmize/Project/spotify-better-taste/logs}"
[[ -d "$LOGS_ROOT" ]] || exit 0

find "$LOGS_ROOT" -name '*.log' ! -name 'launchd-*.log' -mtime +30 -exec gzip -9 {} \; 2>/dev/null || true
find "$LOGS_ROOT" -name '*.log.gz' -mtime +90 -delete 2>/dev/null || true

for f in "$LOGS_ROOT"/launchd-*.log(N); do
  size=$(stat -f%z "$f" 2>/dev/null || echo 0)
  if (( size > 10485760 )); then
    prev=$size
    : > "$f"
    echo "$(date '+%F %T') truncated_at=$(date +%s) prev_size=$prev" >> "$f"
  fi
done
