#!/bin/zsh
# 야간 카탈로그 백필. 예산제라 남은 것은 다음 밤에 이어서 한다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"
bail_if_reauth_required backfill

LOG="$(log_path backfill)"
{
  echo "--- $(date '+%F %T') 백필 시작 ---"
  "$UV_BIN" run spotify-taste backfill run --max-calls 3000
  "$UV_BIN" run spotify-taste backfill status
  echo "--- $(date '+%F %T') 백필 종료 ---"
} >> "$LOG" 2>&1
