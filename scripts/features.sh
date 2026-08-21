#!/bin/zsh
# 야간 취향 특성 재계산 (0층 가중치 · 1층 친화도).
#
# classify(04:40) 뒤에 도는 이유: 가중치가 `context_origin` 을 읽으므로 그날의
# 분류가 끝난 뒤여야 한다. maintenance(05:00) 앞인 이유: 백업에 그날 계산이
# 담기게 하려는 것.
#
# 파라미터 버전은 여기서 만들지 않는다. 매일 새로 만들면 기저율이 매일
# 재동결돼 과거 점수와 비교가 불가능해진다 — 버전 생성은 사람이 정할 일이다.

set -euo pipefail
source "$(dirname "$0")/_common.sh"

LOG="$(log_path features)"
{
  echo "--- $(date '+%F %T') 특성 계산 시작 ---"
  "$UV_BIN" run spotify-taste features build --write
  echo "--- $(date '+%F %T') 특성 계산 종료 ---"
} >> "$LOG" 2>&1
