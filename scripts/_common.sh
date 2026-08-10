# 모든 래퍼가 source 하는 공통 설정.
#
# 무엇/왜:
# - launchd 는 PATH 가 없다. uv 절대경로를 여기 한 곳에서만 정의한다.
# - 날짜별 로그 파일로 리다이렉트한다. 파이썬은 stdout 으로만 찍고
#   로테이션 책임은 셸에 둔다 (rotate-logs.sh).
# - reauth_required 센티넬이 있으면 **exit 0** 으로 빠져나간다. 영구 폐기된
#   토큰으로 KeepAlive 가 재시작 폭풍을 일으키는 것을 막는다.

PROJECT_DIR="/Users/jjmize/Project/spotify-better-taste"
UV_BIN="/opt/homebrew/bin/uv"

cd "$PROJECT_DIR"
mkdir -p logs

log_path() {
  echo "$PROJECT_DIR/logs/$1-$(date +%Y-%m-%d).log"
}

# 재인증이 필요한 상태면 조용히 물러난다 (알림은 이미 갔다).
bail_if_reauth_required() {
  if [[ -f "$PROJECT_DIR/data/auth/reauth_required" ]]; then
    # 백틱을 쓰지 말 것 — 큰따옴표 안에서는 명령 치환이라 실제로
    # `auth login` 이 실행되고 launchd 잡이 브라우저를 띄운다.
    echo "$(date '+%F %T') reauth_required — 'uv run spotify-taste auth login' 필요. 종료." \
      >> "$(log_path "$1")"
    exit 0
  fi
}
