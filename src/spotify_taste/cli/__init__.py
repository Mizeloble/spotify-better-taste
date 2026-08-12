"""무엇/왜
`spotify-taste` 단일 콘솔 엔트리포인트. 모든 운영 동작(수집, 분류, 백필, 진단)은
launchd 래퍼가 아니라 여기의 하위 명령으로 노출된다 — 사람이 손으로 돌리는 것과
자동으로 도는 것이 같은 코드 경로를 타야 재현이 된다.

설계 원칙
- 로깅 설정은 `@app.callback()` 에서 **한 번만** 한다. 하위 명령이 각자 설정하면
  중복 핸들러가 붙는다.
- structlog JSON 을 stdout 으로만 낸다. 파일 핸들러를 파이썬에서 붙이지 않는 이유는
  launchd 래퍼가 날짜별 파일로 리다이렉트하기 때문 — 로테이션 책임을 셸에 둔다.
- **httpx 로거는 강제로 WARNING 으로 내린다.** 형제 프로젝트에서 httpx 의 INFO
  로그가 Telegram `/bot<TOKEN>/sendMessage` 경로를 1,500줄 넘게 찍어 봇 토큰을
  유출한 사고가 있었다. 같은 함정, 같은 방어.

어떻게 확인
    uv run spotify-taste --help
"""

from __future__ import annotations

import logging

import structlog
import typer

from ..config import get_settings
from . import _auth, _backfill, _classify, _collect, _db, _import, _ops

__all__ = ["app"]

app = typer.Typer(
    name="spotify-taste",
    help="Spotify 청취 로그 수집 · 취향 분석 · 주간 추천",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(_auth.app, name="auth")
app.add_typer(_collect.collect_app, name="collect")
app.add_typer(_classify.app, name="classify")
app.add_typer(_collect.sessions_app, name="sessions")
app.add_typer(_backfill.backfill_app, name="backfill")
app.add_typer(_backfill.snapshot_app, name="snapshot")
app.add_typer(_db.app, name="db")
app.add_typer(_import.app, name="import")
app.add_typer(_ops.app, name="ops")


def _setup_logging() -> None:
    """structlog JSON → stdout. 프로세스당 1회."""
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )
    # 토큰 유출 방지 — DEBUG 를 명시적으로 켠 경우에만 httpx 의 요청 URL 이 보인다.
    httpx_level = level if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)


@app.callback()
def main() -> None:
    """모든 하위 명령 실행 전에 공통 초기화."""
    _setup_logging()


@app.command()
def version() -> None:
    """설치된 버전을 출력한다."""
    from .. import __version__

    typer.echo(__version__)
