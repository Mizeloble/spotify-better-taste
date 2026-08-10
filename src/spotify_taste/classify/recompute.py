"""무엇/왜
저장된 세션에 출처 판정을 다시 붙인다. 규칙이 바뀌면 과거 전체를 새 규칙으로
다시 계산하는 경로다 — 이게 되기 때문에 폴러가 `context_origin` 을 건드리지
않는 것이다.

설계 원칙
- **dry-run 이 기본 사고방식이다.** 규칙 하나를 고치기 전에 "1,204행이
  unknown→algorithmic 으로, 3행이 self→external 로 옮겨 간다"를 먼저 본다.
  전이 행렬이 없으면 규칙 변경은 도박이다.
- `--stale` 은 `rules_version < RULES_VERSION` 인 행만 고른다. `RULES_VERSION`
  을 올리는 순간 전 행이 대상이 된다.
- 네트워크를 쓰지 않는다 (플레이리스트 캐시 갱신은 별도 단계). 분류는
  언제든 반복 가능하고 파괴적이지 않아야 한다.

어떻게 확인
    uv run spotify-taste classify run --dry-run
    uv run pytest tests/test_recompute.py
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

import structlog

from .._common import now_ms
from ..db.queries import (
    SELECT_LIBRARY_URIS,
    SELECT_ME_ID,
    SELECT_OWN_PLAYLIST_TRACK_URIS,
    SELECT_PLAYLIST_META,
    SELECT_SESSIONS_FOR_CLASSIFY,
    UPDATE_SESSION_ORIGIN,
)
from .patterns import RULES_VERSION
from .rules import Evidence, Origin, classify

__all__ = ["RecomputeResult", "recompute"]

log = structlog.get_logger(__name__)


@dataclass
class RecomputeResult:
    considered: int = 0
    changed: int = 0
    written: int = 0
    #: (이전 → 이후) 전이 횟수. dry-run 이 이걸 보여 준다.
    transitions: Counter[tuple[str, str]] = field(default_factory=Counter)
    origins: Counter[str] = field(default_factory=Counter)

    def transition_rows(self) -> list[tuple[str, str, int]]:
        """보기 좋은 순서(많이 옮겨 간 것부터)로."""
        return [(a, b, n) for (a, b), n in self.transitions.most_common()]


def recompute(
    conn: sqlite3.Connection,
    *,
    stale_only: bool = True,
    since: int | None = None,
    write: bool = False,
    degraded_mode: bool = False,
) -> RecomputeResult:
    """세션들의 `context_origin` 을 다시 계산한다.

    `write=False` 면 아무것도 저장하지 않고 전이 행렬만 만든다.
    """
    me_row = conn.execute(SELECT_ME_ID).fetchone()
    me_id = str(me_row[0]) if me_row else None
    if me_id is None:
        # `owner.id == me` 를 판정할 수 없으면 내 플레이리스트가 전부
        # external 로 떨어진다. 조용히 진행하면 안 된다.
        log.warning("classify.me_unknown", hint="`spotify-taste snapshot me` 를 먼저 실행하세요")

    library = {str(r[0]) for r in conn.execute(SELECT_LIBRARY_URIS)}
    own_tracks = (
        {str(r[0]) for r in conn.execute(SELECT_OWN_PLAYLIST_TRACK_URIS, (me_id,))}
        if me_id
        else set()
    )
    playlist_cache: dict[str, sqlite3.Row | None] = {}
    result = RecomputeResult()
    now = now_ms()

    for row in conn.execute(SELECT_SESSIONS_FOR_CLASSIFY):
        if since is not None and int(row["started_at"]) < since:
            continue
        version = row["rules_version"]
        if stale_only and version is not None and int(version) >= RULES_VERSION:
            continue

        evidence = _build_evidence(
            conn,
            row,
            me_id=me_id,
            library=library,
            own_tracks=own_tracks,
            cache=playlist_cache,
            degraded_mode=degraded_mode,
        )
        origin, reason = classify(evidence)

        previous = row["previous_origin"] or "(미분류)"
        result.considered += 1
        result.origins[str(origin)] += 1
        if previous != str(origin):
            result.changed += 1
            result.transitions[(previous, str(origin))] += 1

        if write:
            conn.execute(
                UPDATE_SESSION_ORIGIN,
                (str(origin), reason, RULES_VERSION, now, int(row["id"])),
            )
            result.written += 1

    log.info(
        "classify.recomputed",
        considered=result.considered,
        changed=result.changed,
        written=result.written,
        rules_version=RULES_VERSION,
    )
    return result


def _build_evidence(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    me_id: str | None,
    library: set[str],
    own_tracks: set[str],
    cache: dict[str, sqlite3.Row | None],
    degraded_mode: bool,
) -> Evidence:
    context_uri = row["context_uri"]
    meta = None
    if context_uri and (row["context_type"] or "").lower() == "playlist":
        if context_uri not in cache:
            cache[context_uri] = conn.execute(SELECT_PLAYLIST_META, (context_uri,)).fetchone()
        meta = cache[context_uri]

    track_uri = str(row["track_uri"])
    silence = row["silence_before_ms"]
    return Evidence(
        context_uri=context_uri,
        context_type=row["context_type"],
        playlist_owner_id=meta["owner_id"] if meta else None,
        playlist_name=meta["name"] if meta else None,
        playlist_is_generated=bool(meta["is_generated"]) if meta else False,
        playlist_resolved=bool(meta and meta["fetch_status"] == "ok"),
        me_id=me_id,
        track_in_library=track_uri in library,
        track_in_own_playlist=track_uri in own_tracks,
        # 첫 세션은 직전이 없어 NULL 이다. '무한히 조용했다'로 보지 않는다 —
        # 수집을 막 시작한 것일 수도 있기 때문이다.
        silence_before_ms=int(silence) if silence is not None else None,
        shuffle=None if row["shuffle"] is None else bool(row["shuffle"]),
        degraded_mode=degraded_mode,
    )


def origin_labels() -> list[str]:
    return [str(o) for o in Origin]
