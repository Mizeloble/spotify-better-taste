#!/bin/zsh
# 야간 출처 재분류. 규칙이 바뀐 행만 다시 계산한다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"

LOG="$(log_path classify)"
{
  echo "--- $(date '+%F %T') 분류 시작 ---"
  "$UV_BIN" run spotify-taste classify run --stale --write
  "$UV_BIN" run spotify-taste classify report
  echo "--- $(date '+%F %T') 분류 종료 ---"
} >> "$LOG" 2>&1
