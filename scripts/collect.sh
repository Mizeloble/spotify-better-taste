#!/bin/zsh
# 상시 수집기. launchd 가 KeepAlive 로 살려 둔다.
#
# 어떻게 확인:
#   tail -f logs/collect-$(date +%F).log
#   uv run spotify-taste db stats

set -euo pipefail
source "$(dirname "$0")/_common.sh"
bail_if_reauth_required collect

LOG="$(log_path collect)"
echo "--- $(date '+%F %T') 수집 시작 ---" >> "$LOG"

# caffeinate 를 쓰지 않는다: 청취 로거 때문에 Mac 을 24시간 깨워 두는 것은
# 사용자 적대적이고 불필요하다. Mac 이 자면 폰 재생은 recently-played 가
# 메꾼다 — 보조 폴러가 존재하는 주된 이유가 이것이다.
exec "$UV_BIN" run spotify-taste collect >> "$LOG" 2>&1
