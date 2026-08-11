"""Settings 로딩과 파생 프로퍼티."""

from __future__ import annotations

from pathlib import Path

import pytest

from spotify_taste.config import SPOTIFY_SCOPES, Settings, get_settings


def test_defaults_are_usable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` 도 환경변수도 없이 로드된다 — 기본값만으로 `--help` 가 떠야 한다."""
    # autouse 격리 fixture 가 경로들을 tmp_path 로 덮어쓰므로, 기본값을 보려면
    # 그 환경변수들을 걷어내야 한다.
    for key in ("DB_PATH", "BACKUP_DIR", "SPOTIFY_TOKEN_PATH", "HEARTBEAT_PATH", "ALERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(_env_file=None)
    assert s.player_poll_sec == 25
    assert s.db_path == Path("data/spotify-taste.db")
    assert s.spotify_redirect_uri == "http://127.0.0.1:8888/callback"
    assert s.idle_backoff_steps == [25, 45, 90, 120]


def test_idle_backoff_steps_parsed_from_csv() -> None:
    s = Settings(_env_file=None, player_idle_backoff_sec="25,45,90,120")
    assert s.idle_backoff_steps == [25, 45, 90, 120]


def test_idle_backoff_tolerates_whitespace_and_trailing_comma() -> None:
    s = Settings(_env_file=None, player_idle_backoff_sec=" 25 , 45 ,")
    assert s.idle_backoff_steps == [25, 45]


@pytest.mark.parametrize("bad", ["", "0,10", "-5", "abc"])
def test_invalid_idle_backoff_rejected_at_load(bad: str) -> None:
    """새벽 3시 ValueError 대신 기동 시 실패."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, player_idle_backoff_sec=bad)


def test_has_telegram_requires_both_token_and_chat_id() -> None:
    assert not Settings(_env_file=None).has_telegram
    assert not Settings(_env_file=None, telegram_bot_token="t").has_telegram
    assert not Settings(_env_file=None, telegram_chat_id="1").has_telegram
    assert Settings(_env_file=None, telegram_bot_token="t", telegram_chat_id="1").has_telegram


def test_none_paths_mean_feature_disabled() -> None:
    s = Settings(_env_file=None, backup_dir=None, spotify_token_path=None)
    assert not s.has_backup
    assert not s.has_token_file
    # 토큰 파일이 없어도 센티넬을 놓을 곳은 있어야 한다.
    assert s.auth_dir == Path("data/auth")


def test_sentinel_paths_derive_from_configured_locations() -> None:
    s = Settings(
        _env_file=None,
        spotify_token_path=Path("/x/auth/tok.json"),
        heartbeat_path=Path("/y/hb/collector.txt"),
    )
    assert s.reauth_sentinel_path == Path("/x/auth/reauth_required")
    assert s.shutdown_sentinel_path == Path("/y/hb/stopped.txt")


def test_secret_not_exposed_in_repr() -> None:
    """설정 객체를 통째로 로그에 찍어도 토큰이 새지 않아야 한다."""
    s = Settings(_env_file=None, telegram_bot_token="super-secret-value")
    assert "super-secret-value" not in repr(s)


def test_scopes_cover_phase_one_through_six() -> None:
    assert set(SPOTIFY_SCOPES) == {
        "user-read-private",  # /me 의 product 필드가 이것 없이는 null 이다
        "user-read-playback-state",
        "user-read-recently-played",
        "user-top-read",
        "user-library-read",
        "playlist-read-private",
        "playlist-modify-private",
    }
    assert Settings(_env_file=None).scopes == SPOTIFY_SCOPES


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
