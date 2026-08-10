#!/bin/zsh
# 야간 유지보수. 무결성 점검은 일요일에만 (plist 를 7개로 늘리는 대신
# 여기서 요일을 본다 — 세밀한 시간 게이팅은 셸의 몫이다).

set -euo pipefail
source "$(dirname "$0")/_common.sh"

LOG="$(log_path maintenance)"
{
  echo "--- $(date '+%F %T') 유지보수 시작 ---"
  "$UV_BIN" run spotify-taste db backup
  "$UV_BIN" run spotify-taste db prune
  if [[ "$(date +%u)" == "7" ]]; then
    echo "일요일 — 무결성 점검"
    "$UV_BIN" run spotify-taste db integrity-check
  fi
  echo "--- $(date '+%F %T') 유지보수 종료 ---"
} >> "$LOG" 2>&1
