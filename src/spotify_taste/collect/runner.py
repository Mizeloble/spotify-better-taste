"""무엇/왜
수집 프로세스의 조립과 수명 관리. 폴러들이 공유하는 것들(토큰, HTTP 클라이언트,
레이트리미터, DB writer)을 여기서 한 벌만 만든다.

설계 원칙
- **수집기는 한 프로세스다.** `/me/player` 와 `recently-played` 를 별도
  프로세스로 나누면, PKCE 갱신이 돌려주는 회전된 refresh token 을 서로 덮어써
  어느 날 새벽에 `invalid_grant` 로 수집이 통째로 죽는다. asyncio 태스크
  두 개가 하나의 TokenProvider 를 공유하는 구조가 그 사고를 구조적으로
  불가능하게 만든다. 부수 효과로 SQLite writer 도 하나뿐이라 WAL 쓰기 경합이
  사라진다.
- **토큰 폐기는 종료 사유다.** 알림을 보내고, 재인증 센티넬을 남기고,
  정상종료 표식은 **쓰지 않은 채** 종료한다. 그래야 5분 주기
  `heartbeat-check` 가 독립적으로 두 번째 알람을 올린다.

어떻게 확인
    uv run spotify-taste collect --once
    uv run spotify-taste collect
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sqlite3
from dataclasses import dataclass

import structlog

from ..config import Settings, get_settings
from ..db.connection import connect
from ..db.migrate import migrate
from ..heartbeat import Heartbeat
from ..notify import Notifier, build_notifier
from ..spotify.auth import SpotifyAuthRevoked, TokenProvider
from ..spotify.client import SpotifyClient
from ..spotify.ratelimit import RateLimiter
from ..spotify.raw_archive import RawArchive
from .player_poller import PlayerPoller
from .recent_poller import RecentPoller

__all__ = ["EXIT_AUTH_REVOKED", "Collector", "build_collector"]

log = structlog.get_logger(__name__)

#: 토큰이 폐기됐을 때의 종료 코드. 래퍼가 이 값을 보고 재시작을 멈춘다.
EXIT_AUTH_REVOKED = 3


@dataclass
class Collector:
    """조립된 수집기. `aclose()` 로 정리한다."""

    settings: Settings
    conn: sqlite3.Connection
    tokens: TokenProvider
    client: SpotifyClient
    archive: RawArchive
    player: PlayerPoller
    recent: RecentPoller
    heartbeat: Heartbeat
    notifier: Notifier

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            await self.client.aclose()
        with contextlib.suppress(Exception):
            await self.tokens.aclose()
        with contextlib.suppress(Exception):
            self.conn.close()


def build_collector(settings: Settings | None = None) -> Collector:
    """수집에 필요한 것들을 한 벌 만든다.

    공유되는 것: TokenProvider 하나, httpx 클라이언트 하나, 레이트리미터 하나,
    DB writer 커넥션 하나.
    """
    s = settings or get_settings()
    conn = connect(s.db_path)
    migrate(conn)

    notifier = build_notifier(s)
    tokens = TokenProvider(settings=s, notifier=notifier)
    limiter = RateLimiter(max_retry_after_sec=float(s.max_retry_after_sec))
    archive = RawArchive(conn, settings=s, notifier=notifier)
    client = SpotifyClient(token_provider=tokens, limiter=limiter, settings=s)
    heartbeat = Heartbeat(s.heartbeat_path)

    player = PlayerPoller(
        conn=conn,
        client=client,
        archive=archive,
        settings=s,
        on_sample_stored=heartbeat.write,
    )
    recent = RecentPoller(conn=conn, client=client, archive=archive, settings=s)
    return Collector(
        settings=s,
        conn=conn,
        tokens=tokens,
        client=client,
        archive=archive,
        player=player,
        recent=recent,
        heartbeat=heartbeat,
        notifier=notifier,
    )


async def run_once(collector: Collector) -> None:
    """두 수집기를 한 번씩만 돌린다 (진단·스모크 테스트용)."""
    collector.player.recover()
    outcome = await collector.player.tick()
    stats = await collector.recent.poll_once()
    log.info(
        "collect.once_done",
        outcome=str(outcome),
        rp_confirmed=stats.confirmed,
        rp_inserted=stats.inserted,
    )


async def run_forever(collector: Collector) -> int:
    """중지 신호까지 계속 수집한다. 종료 코드를 돌려준다."""
    s = collector.settings
    # 기동 시 지난번 정상 종료 표식을 지운다 — 지금부터는 살아 있어야 한다.
    collector.heartbeat.clear_stopped(s.shutdown_sentinel_path)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    # `except*` 블록 안에서는 return 이 문법적으로 금지돼 있어 플래그로 뺀다.
    revoked = False
    try:
        async with asyncio.TaskGroup() as tg:
            # 한 프로세스의 두 태스크. 공유하는 것은 TokenProvider·httpx·
            # 레이트리미터·DB writer 각각 하나씩이다.
            tg.create_task(collector.player.run(stop))
            tg.create_task(collector.recent.run(stop))
    except* SpotifyAuthRevoked:
        log.error("collect.auth_revoked")
        revoked = True

    if revoked:
        # 사람이 재인증해야만 풀린다. 정상종료 표식을 **쓰지 않는다** —
        # 그래야 5분 주기 heartbeat-check 가 독립적으로 두 번째 알람을 올린다.
        # 알람 하나짜리 설계를 믿지 않는 것이 이 프로젝트의 방침이다.
        return EXIT_AUTH_REVOKED

    collector.heartbeat.mark_stopped(s.shutdown_sentinel_path)
    log.info("collect.stopped_cleanly")
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGINT/SIGTERM 에 협조적으로 반응한다.

    launchd 가 SIGTERM 을 보냈을 때 열린 세션을 닫고 정상 종료 표식을 남길
    기회를 갖기 위해 — 그래야 재시작이 장애 알림을 내지 않는다.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
