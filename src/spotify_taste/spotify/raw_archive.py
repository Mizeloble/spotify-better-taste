"""무엇/왜
API 원본 응답의 2계층 아카이브, 그리고 **응답 모양이 바뀌면 알려 주는 카나리**.

Spotify 는 2024년부터 엔드포인트와 필드를 계속 지우고 있다. 다음 삭제가 언제
올지 모르는 상태에서 "우리가 파싱한 dict" 만 남기면, 필드가 사라진 뒤에는
무엇이 있었는지조차 재구성할 수 없다.

설계 원칙
- **2계층.** `/me/player` 는 하루 1,200건이라 전량 보관하면 90일에 600MB 다.
  그래서 좁은 구조화 테이블(`player_sample`, 행당 ~100B)에 영구 보관하고,
  원문은 (a) 세션 첫 샘플, (b) 1/N 카나리, (c) **처음 보는 모양**만 남긴다.
  나머지 저용량 엔드포인트는 전량 영구 보관한다.
- **모양 지문은 모든 응답에서 계산한다.** 저장을 건너뛴 응답에서도 계산해야
  필드 삭제를 놓치지 않는다. 계산은 키 경로만 보므로 값(개인정보)이 지문에
  섞이지 않는다.
- **처음 보는 모양은 무조건 원문을 남기고 알림을 보낸다.** 필드가 사라진
  바로 그 응답이 증거인데, 그걸 표본에서 빠뜨리면 의미가 없다.
- **아카이브 실패가 수집을 멈추지 않는다.** 호출부(`SpotifyClient`)가 예외를
  삼키지만, 여기서도 방어적으로 쓴다.

어떻게 확인
    uv run pytest tests/test_raw_archive.py tests/test_schema_shape.py
"""

from __future__ import annotations

import sqlite3

import structlog

from .._common import now_ms, sha256_hex, shape_hash
from ..config import Settings, get_settings
from ..db.queries import (
    COUNT_SHAPES_FOR_ENDPOINT,
    INSERT_RAW_RESPONSE,
    SELECT_SHAPE,
    UPDATE_SHAPE_ALERTED,
    UPDATE_SHAPE_SAMPLE,
    UPSERT_SCHEMA_SHAPE,
)
from ..notify import AlertKind, NoopNotifier, Notifier
from .client import SpotifyResponse
from .endpoints import Endpoint

__all__ = ["RawArchive", "RetentionPolicy"]

log = structlog.get_logger(__name__)

DAY_MS = 86_400_000


class RetentionPolicy:
    """엔드포인트별 원문 보관 정책."""

    #: 고volume 이라 표본만 남기는 엔드포인트.
    SAMPLED = frozenset({Endpoint.PLAYER})
    #: 표본으로 남긴 원문의 보관 기간. 모양 확인용이라 오래 둘 필요가 없다.
    SAMPLED_KEEP_MS = 90 * DAY_MS
    #: 저volume 엔드포인트는 영구 보관 (keep_until = NULL).
    #: Phase 3 의 프롬프트 입력을 나중에 재구성하려면 원문이 있어야 한다.


class RawArchive:
    """원문 저장 + 모양 변화 감지."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings or get_settings()
        self._notifier = notifier or NoopNotifier()
        # 엔드포인트별 호출 카운터 (1/N 카나리 판정용).
        self._counters: dict[str, int] = {}

    def store(
        self,
        resp: SpotifyResponse,
        source_job: str,
        *,
        force: bool = False,
    ) -> int | None:
        """원문을 정책에 따라 저장하고 `raw_api_response.id` 를 돌려준다.

        저장하지 않기로 판정하면 None. `force=True` 는 세션 첫 샘플처럼
        반드시 남겨야 하는 경우에 쓴다.
        """
        digest: str | None = None
        shape: str | None = None
        is_new_shape = False

        if resp.json is not None:
            shape, key_paths = shape_hash(resp.json)
            is_new_shape = self._record_shape(resp.endpoint, shape, key_paths)
        if resp.body_text is not None:
            digest = sha256_hex(resp.body_text)

        if not (force or is_new_shape or self._should_sample(resp.endpoint)):
            return None

        # 처음 보는 모양은 영구 보관한다 — 그 응답 자체가 증거다.
        keep_until: int | None = None
        if resp.endpoint in RetentionPolicy.SAMPLED and not is_new_shape:
            keep_until = now_ms() + RetentionPolicy.SAMPLED_KEEP_MS

        cur = self._conn.execute(
            INSERT_RAW_RESPONSE,
            (
                now_ms(),
                resp.endpoint,
                _params_json(resp.params),
                resp.status,
                resp.body_text,
                digest,
                shape,
                source_job,
                keep_until,
            ),
        )
        raw_id = int(cur.lastrowid or 0)

        if is_new_shape and shape is not None:
            self._link_and_alert(resp.endpoint, shape, raw_id)
        return raw_id

    # --- 모양 추적 --------------------------------------------------------

    def _record_shape(self, endpoint: str, shape: str, key_paths: str) -> bool:
        """모양을 기록하고 '처음 보는 것인가'를 돌려준다."""
        now = now_ms()
        row = self._conn.execute(SELECT_SHAPE, (endpoint, shape)).fetchone()
        self._conn.execute(UPSERT_SCHEMA_SHAPE, (endpoint, shape, key_paths, now, now))
        return row is None

    def _link_and_alert(self, endpoint: str, shape: str, raw_id: int) -> None:
        """새 모양에 증거 원문을 연결하고, 진짜 '변화'일 때만 알린다."""
        self._conn.execute(UPDATE_SHAPE_SAMPLE, (raw_id, endpoint, shape))
        known = self._conn.execute(COUNT_SHAPES_FOR_ENDPOINT, (endpoint,)).fetchone()[0]
        log.warning("spotify.schema_shape_new", endpoint=endpoint, shape_hash=shape[:12])

        # 그 엔드포인트를 처음 보는 것뿐이면 변화가 아니다 — 알리지 않는다.
        if known <= 1:
            return
        self._conn.execute(UPDATE_SHAPE_ALERTED, (endpoint, shape))
        self._notifier.send(
            AlertKind.SCHEMA_CHANGED,
            f"응답 모양이 바뀌었습니다: {endpoint}",
            f"처음 보는 키 구성입니다 (shape={shape[:12]}…).\n"
            f"필드가 추가되었거나 **사라졌을** 수 있습니다.\n"
            f"확인:\n"
            f"  SELECT key_paths FROM api_schema_shape\n"
            f"   WHERE endpoint = '{endpoint}' ORDER BY first_seen_at;",
        )

    def _should_sample(self, endpoint: str) -> bool:
        """1/N 카나리 판정. 저volume 엔드포인트는 항상 저장한다."""
        if endpoint not in RetentionPolicy.SAMPLED:
            return True
        every = max(1, self._settings.raw_player_sample_every)
        count = self._counters.get(endpoint, 0) + 1
        self._counters[endpoint] = count
        return count % every == 0


def _params_json(params: dict | None) -> str | None:
    if not params:
        return None
    import json

    return json.dumps(params, ensure_ascii=False, sort_keys=True)
