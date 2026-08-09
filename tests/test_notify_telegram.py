"""알림 채널 — 실패 격리, 종류별 쿨다운, 토큰 유출 방지.

이 파일이 지키는 불변식: **알림이 수집을 멈추지 못한다**, 그리고
**봇 토큰이 어디에도 새지 않는다**.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from spotify_taste.config import Settings
from spotify_taste.notify import (
    AlertKind,
    CooldownGate,
    NoopNotifier,
    TelegramNotifier,
    build_notifier,
)

TOKEN = "123456:SUPER-SECRET-BOT-TOKEN"
CHAT = "987654"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


@pytest.fixture
def notifier(tmp_path):
    return TelegramNotifier(TOKEN, CHAT, CooldownGate(tmp_path / "alerts"))


# --- 전송 -----------------------------------------------------------------


@respx.mock
def test_sends_message_with_chat_id(notifier: TelegramNotifier) -> None:
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert notifier.send(AlertKind.TEST, "제목", "본문") is True
    assert route.called
    payload = route.calls[0].request.content.decode()
    assert CHAT in payload
    assert "제목" in payload


@respx.mock
def test_long_body_is_truncated(notifier: TelegramNotifier) -> None:
    """Telegram 상한(4096자)을 넘기면 400 이 떨어진다."""
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert notifier.send(AlertKind.TEST, "t", "가" * 10_000) is True
    import json

    text = json.loads(route.calls[0].request.content)["text"]
    assert len(text) < 4_096
    assert text.endswith("…(생략)")


# --- 실패 격리 ------------------------------------------------------------


@respx.mock
def test_network_failure_is_swallowed(notifier: TelegramNotifier) -> None:
    """알림 실패가 수집을 멈추면 안 된다."""
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("no route"))
    assert notifier.send(AlertKind.TEST, "t") is False


@respx.mock
def test_http_error_is_swallowed(notifier: TelegramNotifier) -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(401, json={"ok": False}))
    assert notifier.send(AlertKind.TEST, "t") is False


@respx.mock
def test_timeout_is_swallowed(notifier: TelegramNotifier) -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    assert notifier.send(AlertKind.TEST, "t") is False


@respx.mock
def test_failed_send_does_not_stamp_cooldown(tmp_path) -> None:
    """실패했는데 쿨다운이 걸리면 사고가 30분간 묻힌다."""
    gate = CooldownGate(tmp_path / "alerts")
    n = TelegramNotifier(TOKEN, CHAT, gate)
    respx.post(SEND_URL).mock(return_value=httpx.Response(500))
    n.send(AlertKind.COLLECTOR_STALE, "t")
    assert not gate.path_for(AlertKind.COLLECTOR_STALE).exists()


# --- 쿨다운 ---------------------------------------------------------------


@respx.mock
def test_second_send_within_cooldown_suppressed(notifier: TelegramNotifier) -> None:
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert notifier.send(AlertKind.COLLECTOR_STALE, "1") is True
    assert notifier.send(AlertKind.COLLECTOR_STALE, "2") is False
    assert route.call_count == 1


@respx.mock
def test_cooldowns_are_independent_per_kind(notifier: TelegramNotifier) -> None:
    """heartbeat 알림이 스키마 변경 알림을 삼키면 안 된다 — 이 프로젝트에서
    쿨다운 파일을 종류별로 나눈 이유 전체."""
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    assert notifier.send(AlertKind.COLLECTOR_STALE, "수집기 정지") is True
    assert notifier.send(AlertKind.SCHEMA_CHANGED, "응답 모양 변경") is True
    assert notifier.send(AlertKind.AUTH_REVOKED, "토큰 폐기") is True
    assert route.call_count == 3


@respx.mock
def test_zero_cooldown_kind_always_sends(notifier: TelegramNotifier) -> None:
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    for _ in range(3):
        assert notifier.send(AlertKind.DAILY_REPORT, "리포트") is True
    assert route.call_count == 3


def test_cooldown_gate_allows_after_expiry(tmp_path) -> None:
    gate = CooldownGate(tmp_path / "alerts")
    gate.stamp(AlertKind.COLLECTOR_STALE)
    assert gate.should_send(AlertKind.COLLECTOR_STALE, cooldown_sec=1_800) is False
    # mtime 을 과거로 돌려 만료를 흉내낸다 (실제로 30분 기다릴 수는 없다).
    import os

    path = gate.path_for(AlertKind.COLLECTOR_STALE)
    old = path.stat().st_mtime - 3_600
    os.utime(path, (old, old))
    assert gate.should_send(AlertKind.COLLECTOR_STALE, cooldown_sec=1_800) is True


def test_cooldown_clear_resets(tmp_path) -> None:
    gate = CooldownGate(tmp_path / "alerts")
    gate.stamp(AlertKind.AUTH_REVOKED)
    gate.clear(AlertKind.AUTH_REVOKED)
    assert gate.should_send(AlertKind.AUTH_REVOKED, cooldown_sec=1_800) is True


def test_cooldown_unwritable_dir_does_not_raise(tmp_path) -> None:
    """쿨다운 파일을 못 쓴다고 알림 경로가 죽으면 안 된다."""
    blocker = tmp_path / "blocked"
    blocker.write_text("나는 파일이지 디렉터리가 아니다")
    gate = CooldownGate(blocker)
    gate.stamp(AlertKind.TEST)  # 예외가 나면 안 된다
    assert gate.should_send(AlertKind.TEST, cooldown_sec=1_800) is True


def test_kind_with_path_separator_cannot_escape_dir(tmp_path) -> None:
    gate = CooldownGate(tmp_path / "alerts")
    path = gate.path_for("../../etc/passwd")
    assert path.parent == tmp_path / "alerts"


# --- 토큰 유출 방지 -------------------------------------------------------


@respx.mock
def test_token_never_appears_in_repr_or_logs(notifier: TelegramNotifier, caplog) -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError(f"failed to reach {SEND_URL}"))
    assert TOKEN not in repr(notifier)
    with caplog.at_level("DEBUG"):
        notifier.send(AlertKind.TEST, "t")
    assert TOKEN not in caplog.text


@respx.mock
def test_token_not_in_raised_exception_text(notifier: TelegramNotifier) -> None:
    """raise_for_status() 를 쓰면 예외 문자열에 토큰이 담긴 URL 이 들어간다.
    그래서 상태 코드만 보고 직접 처리한다."""
    respx.post(SEND_URL).mock(return_value=httpx.Response(404))
    # 예외 자체가 나오지 않는 것이 정답.
    assert notifier.send(AlertKind.TEST, "t") is False


# --- 팩토리 ---------------------------------------------------------------


def test_factory_returns_noop_when_unconfigured() -> None:
    assert isinstance(build_notifier(Settings(_env_file=None)), NoopNotifier)


def test_factory_returns_noop_when_only_token_set() -> None:
    s = Settings(_env_file=None, telegram_bot_token=TOKEN)
    assert isinstance(build_notifier(s), NoopNotifier)


def test_factory_returns_telegram_when_configured(tmp_path) -> None:
    s = Settings(
        _env_file=None,
        telegram_bot_token=TOKEN,
        telegram_chat_id=CHAT,
        alert_dir=tmp_path / "alerts",
    )
    assert isinstance(build_notifier(s), TelegramNotifier)


def test_noop_notifier_returns_false_and_does_not_raise() -> None:
    assert NoopNotifier().send(AlertKind.TEST, "t", "b") is False


def test_every_alert_kind_has_a_cooldown_entry() -> None:
    """새 종류를 추가하고 COOLDOWNS 에 안 넣으면 기본값으로 조용히 떨어진다 —
    의도한 것인지 확인시키는 테스트."""
    kinds = {
        v for k, v in vars(AlertKind).items() if k.isupper() and isinstance(v, str) and k != "TEST"
    }
    assert kinds <= set(AlertKind.COOLDOWNS)
