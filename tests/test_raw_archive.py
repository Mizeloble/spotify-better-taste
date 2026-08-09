"""원본 아카이브 — 2계층 정책과 모양 변화 카나리.

Spotify 가 계속 필드를 지우는 상황에서, 이 파일이 지키는 건
**"필드가 사라진 그 응답이 증거로 남는다"** 이다.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from spotify_taste.config import Settings
from spotify_taste.db.connection import tx
from spotify_taste.notify import AlertKind
from spotify_taste.spotify.client import SpotifyResponse
from spotify_taste.spotify.endpoints import Endpoint
from spotify_taste.spotify.raw_archive import RawArchive


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, kind: str, title: str, body: str = "") -> bool:
        self.sent.append((kind, title))
        return True

    def kinds(self) -> list[str]:
        return [k for k, _ in self.sent]


def _resp(endpoint: str, payload: dict | None, *, status: int = 200) -> SpotifyResponse:
    text = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    return SpotifyResponse(status, payload, text, endpoint, None)


@pytest.fixture
def archive(db: sqlite3.Connection):
    notifier = RecordingNotifier()
    settings = Settings(_env_file=None, raw_player_sample_every=20)
    return RawArchive(db, settings=settings, notifier=notifier), db, notifier


# --- 저volume 엔드포인트: 전량 영구 보관 ----------------------------------


def test_low_volume_endpoint_always_stored(archive) -> None:
    arc, db, _ = archive
    with tx(db):
        raw_id = arc.store(_resp(Endpoint.TRACK, {"id": "t1"}), "backfill")
    assert raw_id
    row = db.execute("SELECT * FROM raw_api_response WHERE id = ?", (raw_id,)).fetchone()
    assert row["endpoint"] == Endpoint.TRACK
    assert row["source_job"] == "backfill"


def test_low_volume_kept_forever(archive) -> None:
    """다음 필드 삭제가 왔을 때 Phase 3 프롬프트 입력을 재구성하려면
    원문이 남아 있어야 한다."""
    arc, db, _ = archive
    with tx(db):
        raw_id = arc.store(_resp(Endpoint.ARTIST, {"id": "a1", "genres": ["k-indie"]}), "backfill")
    keep_until = db.execute(
        "SELECT keep_until FROM raw_api_response WHERE id = ?", (raw_id,)
    ).fetchone()[0]
    assert keep_until is None


def test_body_stored_verbatim(archive) -> None:
    """파싱된 dict 만 남기면 그 순간의 우리 가정이 섞인다."""
    arc, db, _ = archive
    raw = '{"id":"t1","name":"한글 곡명","weird":  [1,2,3]}'
    resp = SpotifyResponse(200, json.loads(raw), raw, Endpoint.TRACK, None)
    with tx(db):
        raw_id = arc.store(resp, "backfill")
    stored = db.execute("SELECT body FROM raw_api_response WHERE id = ?", (raw_id,)).fetchone()[0]
    assert stored == raw


def test_request_params_recorded(archive) -> None:
    arc, db, _ = archive
    resp = SpotifyResponse(200, {"items": []}, "{}", Endpoint.MY_TRACKS, {"limit": 50, "offset": 0})
    with tx(db):
        raw_id = arc.store(resp, "snapshot")
    params = db.execute(
        "SELECT request_params FROM raw_api_response WHERE id = ?", (raw_id,)
    ).fetchone()[0]
    assert json.loads(params) == {"limit": 50, "offset": 0}


def test_error_response_archived_too(archive) -> None:
    """404 의 원문도 남아야 '왜 못 찾았나'를 나중에 볼 수 있다."""
    arc, db, _ = archive
    with tx(db):
        raw_id = arc.store(
            _resp(Endpoint.TRACK, {"error": {"status": 404}}, status=404), "backfill"
        )
    status = db.execute(
        "SELECT http_status FROM raw_api_response WHERE id = ?", (raw_id,)
    ).fetchone()[0]
    assert status == 404


# --- /me/player: 표본만 --------------------------------------------------


def test_player_responses_are_sampled_not_stored_every_time(archive) -> None:
    """하루 1,200건을 전량 보관하면 90일에 600MB 다."""
    arc, db, _ = archive
    with tx(db):
        stored = [
            arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller") for _ in range(60)
        ]
    kept = [r for r in stored if r is not None]
    # 첫 응답은 '처음 보는 모양'이라 저장되고, 이후 20회마다 카나리.
    assert 3 <= len(kept) <= 5
    assert len(kept) < 60


def test_forced_store_always_kept(archive) -> None:
    """세션 첫 샘플은 표본 주기와 무관하게 남긴다."""
    arc, db, _ = archive
    with tx(db):
        arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller")  # 첫 모양
        forced = arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller", force=True)
    assert forced is not None


def test_sampled_player_bodies_expire(archive) -> None:
    arc, db, _ = archive
    with tx(db):
        arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller")  # 새 모양 → 영구
        forced = arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller", force=True)
    keep_until = db.execute(
        "SELECT keep_until FROM raw_api_response WHERE id = ?", (forced,)
    ).fetchone()[0]
    assert keep_until is not None


def test_sample_interval_is_configurable(db: sqlite3.Connection) -> None:
    arc = RawArchive(db, settings=Settings(_env_file=None, raw_player_sample_every=2))
    with tx(db):
        stored = [arc.store(_resp(Endpoint.PLAYER, {"a": 1}), "poller") for _ in range(10)]
    assert len([r for r in stored if r is not None]) >= 5


# --- 모양 카나리 ----------------------------------------------------------


def test_first_shape_recorded_without_alert(archive) -> None:
    """엔드포인트를 처음 보는 것은 변화가 아니다."""
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "name": "n"}), "backfill")
    assert db.execute("SELECT count(*) FROM api_schema_shape").fetchone()[0] == 1
    assert AlertKind.SCHEMA_CHANGED not in notifier.kinds()


def test_removed_field_triggers_exactly_one_alert(archive) -> None:
    """이 프로젝트에서 필드 삭제를 실시간으로 잡는 장치."""
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "popularity": 50, "name": "n"}), "backfill")
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "name": "n"}), "backfill")  # popularity 삭제
    assert notifier.kinds().count(AlertKind.SCHEMA_CHANGED) == 1
    assert db.execute("SELECT count(*) FROM api_schema_shape").fetchone()[0] == 2


def test_repeated_new_shape_alerts_only_once(archive) -> None:
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "popularity": 1}), "backfill")
        for _ in range(5):
            arc.store(_resp(Endpoint.TRACK, {"id": "t"}), "backfill")
    assert notifier.kinds().count(AlertKind.SCHEMA_CHANGED) == 1


def test_new_shape_is_always_stored_even_when_sampling(archive) -> None:
    """필드가 사라진 바로 그 응답이 증거인데 표본에서 빠지면 의미가 없다."""
    arc, db, _ = archive
    with tx(db):
        arc.store(_resp(Endpoint.PLAYER, {"is_playing": True, "context": {"uri": "u"}}), "poller")
        # 곧바로 다음 응답에서 context 가 사라진다 (카나리 주기와 무관하게 저장돼야).
        changed = arc.store(_resp(Endpoint.PLAYER, {"is_playing": True}), "poller")
    assert changed is not None


def test_new_shape_evidence_is_kept_forever(archive) -> None:
    arc, db, _ = archive
    with tx(db):
        arc.store(_resp(Endpoint.PLAYER, {"a": 1}), "poller")
        changed = arc.store(_resp(Endpoint.PLAYER, {"a": 1, "b": 2}), "poller")
    keep_until = db.execute(
        "SELECT keep_until FROM raw_api_response WHERE id = ?", (changed,)
    ).fetchone()[0]
    assert keep_until is None


def test_shape_row_links_to_evidence(archive) -> None:
    """모양마다 예시 원문이 붙어 있어야 '무엇이 사라졌나'를 볼 수 있다."""
    arc, db, _ = archive
    with tx(db):
        first = arc.store(_resp(Endpoint.TRACK, {"id": "t", "popularity": 1}), "backfill")
        changed = arc.store(_resp(Endpoint.TRACK, {"id": "t"}), "backfill")

    rows = {
        r["sample_raw_id"]: r["alerted"]
        for r in db.execute("SELECT sample_raw_id, alerted FROM api_schema_shape")
    }
    # 두 모양 모두 증거가 붙는다.
    assert set(rows) == {first, changed}
    # 하지만 alerted 는 '실제로 알림을 보냈다'만 뜻한다 — 첫 모양은 변화가 아니다.
    assert rows[first] == 0
    assert rows[changed] == 1


def test_last_seen_updated_on_repeat(archive) -> None:
    arc, db, _ = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t"}), "backfill")
        first = db.execute("SELECT first_seen_at, last_seen_at FROM api_schema_shape").fetchone()
        arc.store(_resp(Endpoint.TRACK, {"id": "t2"}), "backfill")
        after = db.execute("SELECT first_seen_at, last_seen_at FROM api_schema_shape").fetchone()
    assert after["first_seen_at"] == first["first_seen_at"]
    assert after["last_seen_at"] >= first["last_seen_at"]


def test_shapes_tracked_per_endpoint(archive) -> None:
    """`/tracks` 의 변화가 `/artists` 의 알림을 삼키면 안 된다."""
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t"}), "backfill")
        arc.store(_resp(Endpoint.ARTIST, {"id": "a"}), "backfill")
    endpoints = {r[0] for r in db.execute("SELECT endpoint FROM api_schema_shape")}
    assert endpoints == {Endpoint.TRACK, Endpoint.ARTIST}
    assert AlertKind.SCHEMA_CHANGED not in notifier.kinds()


def test_array_length_change_is_not_a_shape_change(archive) -> None:
    """항목 수가 늘었다고 알림이 오면 아무도 알림을 안 본다."""
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.MY_TRACKS, {"items": [{"a": 1}]}), "snapshot")
        arc.store(_resp(Endpoint.MY_TRACKS, {"items": [{"a": 1}, {"a": 2}]}), "snapshot")
    assert AlertKind.SCHEMA_CHANGED not in notifier.kinds()


def test_value_change_is_not_a_shape_change(archive) -> None:
    arc, db, notifier = archive
    with tx(db):
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "name": "곡1"}), "backfill")
        arc.store(_resp(Endpoint.TRACK, {"id": "t", "name": "완전히 다른 이름"}), "backfill")
    assert AlertKind.SCHEMA_CHANGED not in notifier.kinds()


def test_204_has_no_body_and_no_shape(archive) -> None:
    """`/me/player` 의 204 는 본문이 없다. 모양 지문도 없다."""
    arc, db, notifier = archive
    resp = SpotifyResponse(204, None, None, Endpoint.PLAYER, None)
    with tx(db):
        arc.store(resp, "poller", force=True)
    assert db.execute("SELECT count(*) FROM api_schema_shape").fetchone()[0] == 0
    assert AlertKind.SCHEMA_CHANGED not in notifier.kinds()


def test_body_hash_recorded(archive) -> None:
    arc, db, _ = archive
    with tx(db):
        raw_id = arc.store(_resp(Endpoint.TRACK, {"id": "t"}), "backfill")
    digest = db.execute(
        "SELECT body_sha256 FROM raw_api_response WHERE id = ?", (raw_id,)
    ).fetchone()[0]
    assert digest and len(digest) == 64
