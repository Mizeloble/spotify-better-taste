"""무엇/왜
보조 수집기. `GET /me/player/recently-played` 를 10분마다 읽어 주 폴러가
놓친 구간을 메꾼다 — 프로세스가 죽어 있었거나, Mac 이 자는 동안 폰으로 들었거나.

설계 원칙
- **주 폴러를 대체하지 않는다.** 여기에는 `ms_played` 가 없고 `played_at` 은
  30초로 반올림돼 온다. 그래서 이 경로로 만든 세션은 `timing_confidence='low'`
  이고 실측 컬럼은 NULL 이다.
- `after` 커서로 증분 수집한다. 커서는 DB 에 있으므로 재시작을 견딘다.
- **커서를 무조건 전진시키지 않는다.** 병합에 실패하면 다음 실행이 같은
  구간을 다시 읽어야 한다. 커서는 성공한 뒤에만 옮긴다.

어떻게 확인
    uv run pytest tests/test_recent_poller.py
"""

from __future__ import annotations

import asyncio
import sqlite3

import structlog

from .._common import now_ms
from ..config import Settings, get_settings
from ..db.connection import tx
from ..db.queries import GET_CURSOR, SET_CURSOR
from ..sessions.merge import MergeStats, merge_recent_plays
from ..spotify.client import SpotifyApiError, SpotifyClient
from ..spotify.endpoints import Endpoint
from ..spotify.raw_archive import RawArchive
from ..spotify.schemas import parse_recently_played

__all__ = ["CURSOR_NAME", "RecentPoller"]

log = structlog.get_logger(__name__)

CURSOR_NAME = "recently_played_after"
#: 한 페이지 최대치. 이 엔드포인트는 아직 50 을 허용한다.
PAGE_LIMIT = 50
#: 슬롯을 못 얻으면 그냥 다음 주기를 기다린다 — 급할 것 없는 보조 경로다.
SLOT_TIMEOUT_SEC = 10.0


class RecentPoller:
    """recently-played 증분 수집 + 병합."""

    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        client: SpotifyClient,
        archive: RawArchive,
        settings: Settings | None = None,
    ) -> None:
        self._conn = conn
        self._client = client
        self._archive = archive
        self._settings = settings or get_settings()

    # --- 커서 -------------------------------------------------------------

    def _get_cursor(self) -> int | None:
        row = self._conn.execute(GET_CURSOR, (CURSOR_NAME,)).fetchone()
        if row is None:
            return None
        try:
            return int(row["value"])
        except TypeError, ValueError:
            return None

    def _set_cursor(self, value: int) -> None:
        self._conn.execute(SET_CURSOR, (CURSOR_NAME, str(value), now_ms()))

    # --- 한 회 -----------------------------------------------------------

    async def poll_once(self) -> MergeStats:
        """한 페이지를 읽어 병합한다. 예외를 밖으로 내지 않는다."""
        params: dict[str, object] = {"limit": PAGE_LIMIT}
        cursor = self._get_cursor()
        if cursor is not None:
            # `after` 는 이 시각 **이후**의 재생만 달라는 뜻이다.
            params["after"] = cursor

        try:
            resp = await self._client.get(
                Endpoint.RECENTLY_PLAYED,
                params=params,
                role="recent",
                source_job="recent",
                acquire_timeout=SLOT_TIMEOUT_SEC,
            )
        except SpotifyApiError as exc:
            log.warning("recent.fetch_failed", status=exc.status)
            return MergeStats()

        if resp is None or resp.json is None:
            return MergeStats()

        plays = parse_recently_played(resp.json)
        if not plays:
            return MergeStats()

        with tx(self._conn):
            stats = merge_recent_plays(self._conn, plays)
            # 병합이 성공한 뒤에만 커서를 옮긴다.
            self._set_cursor(max(p.played_at for p in plays))
        return stats

    async def run(self, stop: asyncio.Event) -> None:
        """중지 신호까지 주기적으로 돈다."""
        interval = float(self._settings.recent_poll_sec)
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue
