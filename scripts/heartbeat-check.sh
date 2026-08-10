#!/bin/zsh
# Dead-man switch. 5분마다 수집기가 살아 있는지 확인한다.
# 요일/시간 게이트가 없는 이유: 멈춘 수집기는 **언제든** 비정상이다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"

# 종료 코드 1 은 '장애 감지'다. launchd 는 이걸 실패로 보지만 재시작하지
# 않으므로(StartInterval 잡) 문제없다.
"$UV_BIN" run spotify-taste ops heartbeat-check --quiet >> "$(log_path heartbeat)" 2>&1 || true
