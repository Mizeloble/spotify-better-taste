#!/bin/zsh
# 아침 수집 리포트. 문제가 있으면 Telegram 으로도 보낸다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"

"$UV_BIN" run spotify-taste ops report --days 1 --notify \
  >> "$(log_path daily-report)" 2>&1 || true
