#!/bin/zsh
# 야간 스냅샷. /me/top 은 현재 상태만 주므로 찍지 않은 주는 영구 소실이다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"
bail_if_reauth_required snapshot

LOG="$(log_path snapshot)"
{
  echo "--- $(date '+%F %T') 스냅샷 시작 ---"
  "$UV_BIN" run spotify-taste snapshot all
  echo "--- $(date '+%F %T') 스냅샷 종료 ---"
} >> "$LOG" 2>&1
