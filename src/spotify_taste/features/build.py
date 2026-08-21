"""무엇/왜
0층(재생별 가중치)과 1층(엔티티 친화도)을 계산해 적재하는 드라이버.

`weights.py` 가 순수 계산이고 여기는 DB 를 아는 쪽이다 — 읽고, 계산을
맡기고, 쓴다. 분류기의 `recompute.py` 와 같은 역할 분담이다.

설계 원칙
- **파라미터 버전이 모든 파생 행에 박힌다.** 반감기나 기저율이 바뀌면 점수의
  의미가 바뀐다. 버전이 없으면 6개월 뒤 "이 점수는 어떤 설정으로 나온 건가"에
  답할 수 없고, 그러면 재계산도 비교도 불가능하다.
- **기저율은 파라미터를 만들 때 한 번 굳힌다.** 매번 다시 재면 과거 점수가
  소급해 바뀐다.
- **완주도를 모르는 재생은 0 이 아니라 '모름'이다.** 백필이 덜 된 트랙이
  아직 31% 라, 이걸 0 으로 처리하면 롱테일 전체를 "싫어함"으로 학습한다.
- 쓰기는 `--write` 없이는 하지 않는다 (cli/ 규약).

어떻게 확인
    uv run spotify-taste features params --new
    uv run spotify-taste features build --write
    uv run spotify-taste features top artist --horizon core
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

import structlog

from .._common import now_ms
from ..db.connection import tx
from ..db.queries import (
    AGGREGATE_ALBUM_AFFINITY,
    AGGREGATE_ARTIST_AFFINITY,
    AGGREGATE_TRACK_AFFINITY,
    INSERT_FEATURE_PARAMS,
    SELECT_FEATURE_PARAMS,
    SELECT_GLOBAL_BASE_RATE,
    SELECT_LATEST_FEATURE_PARAMS,
    SELECT_MODE_BASE_RATES,
    SELECT_SESSIONS_FOR_WEIGHTS,
    UPSERT_ENTITY_AFFINITY,
    UPSERT_PLAY_WEIGHT,
)
from .weights import DEFAULT_PARAMS, FeatureParams, compute_weight, resolve_completion

__all__ = ["BuildResult", "build_features", "create_params_version", "load_params"]

log = structlog.get_logger(__name__)

#: 부가 아티스트(피처링)의 몫. 주 아티스트가 아닌 이름에 같은 무게를 주면
#: 한 번 스친 피처링이 취향으로 올라온다.
FEATURED_SHARE = 0.5


@dataclass
class BuildResult:
    params_version: int
    sessions: int = 0
    weighted: int = 0  # 완주도를 알아 실제로 점수가 붙은 재생
    unknown_completion: int = 0
    artists: int = 0
    tracks: int = 0
    albums: int = 0


def create_params_version(
    conn: sqlite3.Connection, *, note: str | None = None, base: FeatureParams | None = None
) -> int:
    """현재 데이터에서 기저율을 유도해 새 파라미터 버전을 만든다.

    표본이 30건 미만인 모드는 넣지 않는다 — 몇 건짜리 평균을 기저율로 쓰면
    그 모드의 재생이 통째로 왜곡된다.
    """
    rows = conn.execute(SELECT_MODE_BASE_RATES).fetchall()
    base_rates = {str(r["mode"]): float(r["base_rate"]) for r in rows if r["mode"] is not None}
    global_row = conn.execute(SELECT_GLOBAL_BASE_RATE).fetchone()
    template = base or DEFAULT_PARAMS
    params = FeatureParams(
        **{
            **asdict(template),
            "mode_base_rates": base_rates,
            "global_base_rate": float(global_row["base_rate"] or template.global_base_rate),
        }
    )

    latest = conn.execute(SELECT_LATEST_FEATURE_PARAMS).fetchone()
    version = int(latest["version"]) + 1 if latest else 1
    with tx(conn):
        conn.execute(
            INSERT_FEATURE_PARAMS,
            (version, json.dumps(asdict(params), ensure_ascii=False), note, now_ms()),
        )
    log.info("features.params_created", version=version, modes=len(base_rates))
    return version


def load_params(conn: sqlite3.Connection, version: int | None = None) -> tuple[int, FeatureParams]:
    """버전을 지정하지 않으면 최신을 쓴다. 하나도 없으면 만들지 않고 알린다."""
    row = (
        conn.execute(SELECT_FEATURE_PARAMS, (version,)).fetchone()
        if version is not None
        else conn.execute(SELECT_LATEST_FEATURE_PARAMS).fetchone()
    )
    if row is None:
        raise LookupError("파라미터 버전이 없습니다 — `features params --new` 를 먼저 실행하세요")
    raw = json.loads(row["params_json"])
    raw["deliberate_modes"] = tuple(raw.get("deliberate_modes") or ())
    return int(row["version"]), FeatureParams(**raw)


def build_features(
    conn: sqlite3.Connection, *, params_version: int | None = None, write: bool = False
) -> BuildResult:
    """0층과 1층을 한 번에 계산한다. `write=False` 면 세어 보기만 한다."""
    version, params = load_params(conn, params_version)
    result = BuildResult(params_version=version)
    now = now_ms()

    rows = conn.execute(SELECT_SESSIONS_FOR_WEIGHTS).fetchall()
    payload = []
    for row in rows:
        result.sessions += 1
        completion, source = resolve_completion(
            measured=row["completion_ratio"],
            ms_played=row["ms_played"],
            duration_ms=row["duration_ms"],
            inferred=row["inferred_completion_ratio"],
        )
        if completion is None:
            result.unknown_completion += 1
        else:
            result.weighted += 1

        weight = compute_weight(
            completion=completion,
            completion_source=source,
            mode=_mode_of(row),
            context_origin=row["context_origin"],
            age_ms=now - int(row["started_at"]),
            params=params,
        )
        payload.append(
            (
                int(row["id"]),
                version,
                weight.completion,
                weight.completion_source,
                weight.mode,
                weight.mode_score,
                weight.voluntary,
                weight.weight_current,
                weight.weight_core,
                now,
            )
        )

    if not write:
        log.info("features.build_dry_run", version=version, sessions=result.sessions)
        return result

    with tx(conn):
        conn.executemany(UPSERT_PLAY_WEIGHT, payload)

    result.artists = _aggregate(
        conn, "artist", AGGREGATE_ARTIST_AFFINITY, version, now, featured=True
    )
    result.tracks = _aggregate(conn, "track", AGGREGATE_TRACK_AFFINITY, version, now)
    result.albums = _aggregate(conn, "album", AGGREGATE_ALBUM_AFFINITY, version, now)
    log.info(
        "features.built",
        version=version,
        sessions=result.sessions,
        weighted=result.weighted,
        artists=result.artists,
    )
    return result


def _mode_of(row: sqlite3.Row) -> str | None:
    """재생이 어떻게 시작됐나.

    `reason_start` 는 export 전용이라 라이브 세션에는 없다. 폴러의
    `end_reason` 으로 비슷한 것을 만들 수는 있지만 산출 방식이 달라
    같은 기저율을 쓸 수 없으므로, 접두사를 붙여 **다른 모드로 취급**한다.
    섞으면 2026-08-09 임포트 경계에서 가짜 불연속이 생긴다.
    """
    if row["reason_start"]:
        return str(row["reason_start"])
    if row["source"] == "poller" and row["end_reason"]:
        return f"live:{row['end_reason']}"
    return None


def _aggregate(
    conn: sqlite3.Connection,
    entity_type: str,
    query: str,
    version: int,
    now: int,
    *,
    featured: bool = False,
) -> int:
    args = (FEATURED_SHARE, FEATURED_SHARE, version) if featured else (version,)
    rows = conn.execute(query, args).fetchall()
    with tx(conn):
        for row in rows:
            for horizon, score in (
                ("current", row["score_current"]),
                ("core", row["score_core"]),
            ):
                conn.execute(
                    UPSERT_ENTITY_AFFINITY,
                    (
                        entity_type,
                        row["entity_key"],
                        horizon,
                        version,
                        float(score or 0.0),
                        int(row["plays"]),
                        int(row["measured_plays"] or 0),
                        now,
                    ),
                )
    return len(rows)
