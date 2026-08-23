"""주 폴러 — 적재 순서, 백오프 분리, 재시작 복구.

여기서 지키는 불변식: **저장이 먼저, 해석은 그 다음**, 그리고
**끊긴 구간은 반드시 기록으로 남는다**.
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest
import respx

from spotify_taste.clock import FrozenClock
from spotify_taste.collect.player_poller import (
    RESUME_WINDOW_MS,
    PlayerPoller,
    TickOutcome,
)
from spotify_taste.config import Settings
from spotify_taste.db.connection import tx
from spotify_taste.sessions.store import upsert_open_session
from spotify_taste.spotify.auth import TOKEN_URL, SpotifyAuthRevoked, TokenProvider
from spotify_taste.spotify.client import SpotifyClient
from spotify_taste.spotify.endpoints import API_BASE, Endpoint
from spotify_taste.spotify.ratelimit import RateLimiter
from spotify_taste.spotify.raw_archive import RawArchive
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

PLAYER_URL = f"{API_BASE}{Endpoint.PLAYER}"
NOW = 1_800_000_000_000
A = "spotify:track:AAAA"
B = "spotify:track:BBBB"


def _player_body(track_uri: str, progress_ms: int, *, is_playing: bool = True) -> dict:
    return {
        "is_playing": is_playing,
        "progress_ms": progress_ms,
        "shuffle_state": False,
        "repeat_state": "off",
        "currently_playing_type": "track",
        "timestamp": NOW,
        "device": {"id": "d1", "type": "Computer", "is_private_session": False},
        "context": {"uri": "spotify:playlist:PL", "type": "playlist"},
        "item": {"uri": track_uri, "duration_ms": 180_000, "name": "곡"},
    }


@pytest.fixture
def poller(db: sqlite3.Connection, tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        spotify_client_id="cid",
        spotify_token_path=tmp_path / "token.json",
        player_poll_sec=25,
        player_idle_backoff_sec="25,45,90,120",
        raw_player_sample_every=20,
    )
    store = TokenStore(path=settings.spotify_token_path)
    store.save(
        TokenBundle(
            client_id="cid",
            access_token="TOK",
            refresh_token="REF",
            expires_at_ms=NOW + 3_600_000,
            obtained_at_ms=NOW,
        )
    )
    clock = FrozenClock(start_ms=NOW, start_monotonic=100.0)

    async def no_sleep(seconds: float) -> None:
        clock.advance(seconds=seconds)

    monkeypatch.setattr("spotify_taste.spotify.client.asyncio.sleep", no_sleep)

    provider = TokenProvider(settings=settings, store=store, clock=clock)
    limiter = RateLimiter(clock=clock, sleep=no_sleep)
    archive = RawArchive(db, settings=settings)
    client = SpotifyClient(token_provider=provider, limiter=limiter, settings=settings)
    beats: list[int] = []
    p = PlayerPoller(
        conn=db,
        client=client,
        archive=archive,
        settings=settings,
        on_sample_stored=lambda: beats.append(1),
    )
    return p, db, beats, client


# --- 적재 -----------------------------------------------------------------


@respx.mock
async def test_tick_stores_a_sample(poller) -> None:
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 5_000)))
    assert await p.tick() is TickOutcome.PLAYING
    row = db.execute("SELECT * FROM player_sample").fetchone()
    assert row["track_uri"] == A
    assert row["progress_ms"] == 5_000
    assert row["device_type"] == "Computer"
    await client.aclose()


@respx.mock
async def test_open_session_persisted_every_tick(poller) -> None:
    """프로세스가 언제 죽어도 마지막 관측 상태가 남아야 한다."""
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 5_000)))
    await p.tick()
    row = db.execute("SELECT track_uri, is_open FROM play_session").fetchone()
    assert (row["track_uri"], row["is_open"]) == (A, 1)
    await client.aclose()


@respx.mock
async def test_204_recorded_as_idle_not_error(poller) -> None:
    """204 는 정상 상태다."""
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    assert await p.tick() is TickOutcome.IDLE
    row = db.execute("SELECT http_status, track_uri FROM player_sample").fetchone()
    assert (row["http_status"], row["track_uri"]) == (204, None)
    await client.aclose()


@respx.mock
async def test_sample_linked_to_archived_body(poller) -> None:
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 1_000)))
    await p.tick()
    raw_id = db.execute("SELECT raw_id FROM player_sample").fetchone()[0]
    assert raw_id is not None  # 세션 첫 샘플은 원본이 강제 보관된다
    await client.aclose()


@respx.mock
async def test_heartbeat_only_after_successful_store(poller) -> None:
    """토큰이 죽은 채 도는 프로세스가 heartbeat 를 영원히 쓰면 안 된다 (C7)."""
    p, _, beats, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    await p.tick()
    assert beats == []  # 오류 틱은 살아 있다고 말하지 않는다

    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 0)))
    await p.tick()
    assert len(beats) == 1
    await client.aclose()


# --- 세션 진행 ------------------------------------------------------------


@respx.mock
async def test_track_change_closes_session(poller) -> None:
    p, db, _, client = poller
    route = respx.get(PLAYER_URL)
    route.side_effect = [
        httpx.Response(200, json=_player_body(A, 0)),
        httpx.Response(200, json=_player_body(A, 170_000)),
        httpx.Response(200, json=_player_body(B, 0)),
    ]
    for _ in range(3):
        await p.tick()

    closed = db.execute(
        "SELECT track_uri, end_reason, is_open FROM play_session WHERE is_open = 0"
    ).fetchone()
    assert (closed["track_uri"], closed["end_reason"]) == (A, "track_change")
    await client.aclose()


# --- 오류 -----------------------------------------------------------------


@respx.mock
async def test_api_error_records_gap_and_backs_off(poller) -> None:
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    assert await p.tick() is TickOutcome.ERROR
    reason = db.execute("SELECT reason FROM collector_gap").fetchone()
    # 첫 틱은 이전 시각이 없어 공백 길이가 0 이라 기록되지 않을 수 있다.
    assert reason is None or reason[0] == "error_backoff"
    await client.aclose()


@respx.mock
async def test_error_does_not_close_open_session(poller) -> None:
    """오류 구간은 아무것도 알 수 없다. 세션을 닫으면 청취가 잘린다."""
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 0)))
    await p.tick()

    # 5xx 는 클라이언트가 자체 재시도하므로 응답을 지속적으로 내야 한다.
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    await p.tick()
    await p.tick()

    assert db.execute("SELECT count(*) FROM play_session WHERE is_open = 1").fetchone()[0] == 1
    await client.aclose()


@respx.mock
async def test_error_sample_is_recorded(poller) -> None:
    """오류도 원시 기록으로 남아야 재구성이 실시간과 같아진다."""
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    await p.tick()
    row = db.execute("SELECT http_status FROM player_sample").fetchone()
    assert row["http_status"] == 0
    await client.aclose()


def _expire_token(tmp_path) -> None:
    """디스크의 토큰을 만료시킨다 — 다음 호출이 갱신을 타게 만든다."""
    TokenStore(path=tmp_path / "token.json").save(
        TokenBundle(
            client_id="cid",
            access_token="TOK",
            refresh_token="REF",
            expires_at_ms=NOW - 1,
            obtained_at_ms=NOW - 3_600_000,
        )
    )


@respx.mock
async def test_auth_refresh_failure_is_a_tick_error_not_a_crash(poller, tmp_path) -> None:
    """2026-08-22 11:55~21:15, 네트워크가 끊기자 토큰 갱신 실패가 TaskGroup 을
    무너뜨려 9시간 20분 동안 558회 재시작만 반복했다. 같은 전송 오류라도 API
    호출 쪽은 이미 흡수되고 있었다 — 갱신이 `request()` 의 재시도 루프 밖에서
    일어나 혼자 새어 나갔다. 틱 하나를 잃는 것은 괜찮지만 루프는 살아야 한다.
    """
    p, db, _, client = poller
    _expire_token(tmp_path)
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))

    assert await p.tick() is TickOutcome.ERROR
    # 끊긴 구간은 반드시 기록으로 남는다 — 오류 샘플도 그 일부다.
    assert db.execute("SELECT http_status FROM player_sample").fetchone()["http_status"] == 0
    await client.aclose()


@respx.mock
async def test_auth_revoked_still_escapes(poller, tmp_path) -> None:
    """일시 실패를 흡수하느라 폐기까지 삼키면 아무도 재인증을 부르지 못한다."""
    p, _, _, client = poller
    _expire_token(tmp_path)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    with pytest.raises(SpotifyAuthRevoked):
        await p.tick()
    await client.aclose()


@respx.mock
async def test_rate_limit_skip_records_gap(poller) -> None:
    """폴러는 백필 뒤에 줄서지 않는다 — 건너뛰고 공백을 남긴다."""
    p, db, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 0)))
    await p.tick()  # last_tick_at 을 채운다
    client.limiter.note_rate_limited(120.0)
    assert await p.tick() is TickOutcome.SKIPPED
    reasons = [r[0] for r in db.execute("SELECT reason FROM collector_gap")]
    assert "rate_limit" in reasons
    await client.aclose()


# --- 백오프 ---------------------------------------------------------------


@respx.mock
async def test_idle_backoff_climbs_then_caps(poller) -> None:
    p, _, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    delays = []
    for _ in range(6):
        delays.append(p.next_delay_sec(await p.tick()))
    assert delays[:4] == [25.0, 45.0, 90.0, 120.0]
    assert delays[4:] == [120.0, 120.0]  # 상한에서 멈춘다
    await client.aclose()


@respx.mock
async def test_one_playing_sample_resets_idle_backoff(poller) -> None:
    """무음 뒤 첫 재생 샘플이 간격을 즉시 되돌려야 청취 시작을 놓치지 않는다."""
    p, _, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    for _ in range(4):
        await p.tick()

    respx.get(PLAYER_URL).mock(return_value=httpx.Response(200, json=_player_body(A, 0)))
    assert p.next_delay_sec(await p.tick()) == 25.0
    await client.aclose()


@respx.mock
async def test_error_backoff_is_separate_from_idle(poller) -> None:
    """오류를 유휴로 착각하면 활발한 청취 중에 조용히 폴링이 느려진다."""
    p, _, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    for _ in range(3):
        await p.tick()  # 유휴 인덱스 3

    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    assert p.next_delay_sec(await p.tick()) == 5.0  # 오류 백오프 첫 단계
    await client.aclose()


@respx.mock
async def test_error_does_not_reset_idle_backoff(poller) -> None:
    """오류는 유휴 여부를 알려 주지 않는다. 여전히 조용하다면 간격을 다시
    좁힐 이유가 없다 — 되돌리는 것은 **재생 샘플**뿐이다."""
    p, _, _, client = poller
    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    for _ in range(4):
        await p.tick()  # 유휴 상한(120초)까지 올라간다

    respx.get(PLAYER_URL).mock(return_value=httpx.Response(500))
    await p.tick()

    respx.get(PLAYER_URL).mock(return_value=httpx.Response(204))
    assert p.next_delay_sec(await p.tick()) == 120.0
    await client.aclose()


# --- 재시작 복구 ----------------------------------------------------------


def _seed_open_session(db: sqlite3.Connection, *, last_sample_at: int) -> None:
    from spotify_taste.sessions.model import OpenSession

    with tx(db):
        upsert_open_session(
            db,
            OpenSession(
                track_uri=A,
                started_at=last_sample_at - 60_000,
                duration_ms=180_000,
                first_progress_ms=0,
                last_progress_ms=60_000,
                last_sample_at=last_sample_at,
                last_progress_change_at=last_sample_at,
                ms_played_accum=60_000,
                max_progress_ms=60_000,
                sample_count=3,
            ),
        )


def test_recent_half_open_session_is_resumed(poller) -> None:
    """재시작이 한 곡을 두 세션으로 갈라 놓으면 안 된다."""
    p, db, _, _ = poller
    _seed_open_session(db, last_sample_at=NOW - 30_000)
    p.recover(now=NOW)
    assert p.machine.open_session is not None
    assert p.machine.open_session.track_uri == A
    assert p.machine.open_session.ms_played_accum == 60_000


def test_stale_half_open_session_is_finalized(poller) -> None:
    """공백이 길면 그 사이 무슨 일이 있었는지 알 수 없다."""
    p, db, _, _ = poller
    _seed_open_session(db, last_sample_at=NOW - RESUME_WINDOW_MS - 60_000)
    p.recover(now=NOW)
    assert p.machine.open_session is None
    row = db.execute("SELECT is_open, end_reason FROM play_session").fetchone()
    assert (row["is_open"], row["end_reason"]) == (0, "process_restart")


def test_recovery_records_a_gap_either_way(poller) -> None:
    """모르는 것과, 모른다는 사실을 기록하는 것은 다르다."""
    p, db, _, _ = poller
    _seed_open_session(db, last_sample_at=NOW - 30_000)
    p.recover(now=NOW)
    reasons = [r[0] for r in db.execute("SELECT reason FROM collector_gap")]
    assert reasons == ["process_restart"]


def test_recovery_with_no_open_sessions_is_noop(poller) -> None:
    p, db, _, _ = poller
    p.recover(now=NOW)
    assert p.machine.open_session is None
    assert db.execute("SELECT count(*) FROM collector_gap").fetchone()[0] == 0


def test_abandoned_session_row_is_kept_not_deleted(poller) -> None:
    """실제로 재생된 구간이다. end_reason 으로 신뢰도를 낮출 뿐 지우지 않는다."""
    p, db, _, _ = poller
    _seed_open_session(db, last_sample_at=NOW - 600_000)
    p.recover(now=NOW)
    assert db.execute("SELECT count(*) FROM play_session").fetchone()[0] == 1


async def test_db_lock_does_not_kill_the_run_loop(poller, monkeypatch) -> None:
    """락 한 번에 프로세스가 죽으면 launchd 재시작까지 수집이 통째로 멈춘다.

    2026-08-13 새벽, 백필과 겹친 `database is locked` 가 TaskGroup 을 무너뜨려
    수집 프로세스가 종료됐다. 틱 하나를 잃는 것은 괜찮지만 루프는 살아야 한다.
    """
    p, _, _, client = poller
    calls = 0

    async def boom() -> TickOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        stop.set()
        return TickOutcome.IDLE

    stop = asyncio.Event()
    monkeypatch.setattr(p, "tick", boom)
    monkeypatch.setattr(asyncio, "wait_for", _immediate_timeout)

    await p.run(stop)  # 예외가 새어 나오면 실패다
    assert calls == 2
    await client.aclose()


async def _immediate_timeout(awaitable, timeout):
    """대기 없이 다음 틱으로 넘어간다 (백오프 시간을 실제로 자지 않는다)."""
    awaitable.close()
    raise TimeoutError


async def test_host_sleep_recorded_as_its_own_gap(poller, monkeypatch) -> None:
    """맥이 잠들면 대기가 의도보다 시간 단위로 길어진다. 크래시 공백과
    사유를 갈라 둬야 소크 판정이 슬립을 빼고 볼 수 있다 (2026-08-14 결정)."""
    p, db, _, client = poller
    stop = asyncio.Event()
    ticks = 0

    async def fake_tick() -> TickOutcome:
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            stop.set()
        return TickOutcome.IDLE

    clock = {"t": NOW}

    async def sleepy_wait(awaitable, timeout):
        awaitable.close()
        clock["t"] += 7_200_000  # 두 시간을 잤다 깨어난다
        raise TimeoutError

    monkeypatch.setattr(p, "tick", fake_tick)
    monkeypatch.setattr("spotify_taste.collect.player_poller.now_ms", lambda: clock["t"])
    monkeypatch.setattr(asyncio, "wait_for", sleepy_wait)

    await p.run(stop)
    rows = db.execute("SELECT reason, ended_at - started_at FROM collector_gap").fetchall()
    assert rows and {r[0] for r in rows} == {"host_sleep"}
    assert all(r[1] == 7_200_000 for r in rows)
    await client.aclose()


async def test_normal_wait_records_no_sleep_gap(poller, monkeypatch) -> None:
    """이벤트 루프가 몇 초 늦는 것은 슬립이 아니다."""
    p, db, _, client = poller
    stop = asyncio.Event()

    async def fake_tick() -> TickOutcome:
        stop.set()
        return TickOutcome.IDLE

    monkeypatch.setattr(p, "tick", fake_tick)
    monkeypatch.setattr(asyncio, "wait_for", _immediate_timeout)

    await p.run(stop)
    assert db.execute("SELECT count(*) FROM collector_gap").fetchone()[0] == 0
    await client.aclose()
