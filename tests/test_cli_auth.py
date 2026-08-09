"""auth CLI — 종료 코드와 안내 메시지.

CLI 는 사람이 막혔을 때 "무엇을 해야 하는지" 를 알려 주는 것이 일이다.
종료 코드는 셸 래퍼가 게이트로 쓴다.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from spotify_taste.cli import app
from spotify_taste.cli._auth import _parse_redirect
from spotify_taste.config import get_settings
from spotify_taste.spotify.token_store import TokenBundle, TokenStore

runner = CliRunner()


def _save_bundle(**over) -> TokenBundle:
    settings = get_settings()
    fields: dict = {
        "client_id": settings.spotify_client_id,
        "access_token": "A",
        "refresh_token": "R",
        "expires_at_ms": 4_000_000_000_000,
        "scopes": settings.scopes,
        "obtained_at_ms": 0,
    }
    fields.update(over)
    bundle = TokenBundle(**fields)
    TokenStore(path=settings.spotify_token_path).save(bundle)
    return bundle


# --- status ---------------------------------------------------------------


def test_status_without_token_exits_nonzero() -> None:
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 1
    assert "auth login" in result.output


def test_status_with_valid_token_succeeds() -> None:
    _save_bundle()
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "scope" in result.output


def test_status_reports_missing_scopes() -> None:
    """scope 가 모자라면 특정 엔드포인트만 403 이 나서 원인 찾기가 어렵다."""
    _save_bundle(scopes=("user-top-read",))
    result = runner.invoke(app, ["auth", "status"])
    assert "누락" in result.output
    assert "user-read-playback-state" in result.output


def test_status_never_prints_the_tokens() -> None:
    _save_bundle(access_token="ACCESS-SECRET", refresh_token="REFRESH-SECRET")
    result = runner.invoke(app, ["auth", "status"])
    assert "ACCESS-SECRET" not in result.output
    assert "REFRESH-SECRET" not in result.output


def test_status_flags_reauth_sentinel() -> None:
    """센티넬이 있으면 토큰이 남아 있어도 재인증 상태다."""
    settings = get_settings()
    _save_bundle()
    settings.reauth_sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    settings.reauth_sentinel_path.write_text("revoked")

    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 1
    assert "재인증 필요" in result.output


def test_expired_access_token_is_not_an_error() -> None:
    """액세스 토큰 만료는 정상이다 — 폴러가 갱신한다. 여기서 exit 1 을 내면
    래퍼가 멀쩡한 설치를 고장 났다고 판단한다."""
    _save_bundle(expires_at_ms=1)
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "자동 갱신" in result.output


# --- login ----------------------------------------------------------------


def test_login_without_client_id_exits_with_guidance(monkeypatch) -> None:
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    get_settings.cache_clear()
    result = runner.invoke(app, ["auth", "login"])
    assert result.exit_code == 1
    # rich 가 터미널 폭에 맞춰 줄바꿈하므로 공백을 정규화해 비교한다.
    flat = " ".join(result.output.split())
    assert "SPOTIFY_CLIENT_ID" in flat
    assert "Client Secret 은 필요 없습니다" in flat


# --- logout ---------------------------------------------------------------


def test_logout_removes_token() -> None:
    settings = get_settings()
    _save_bundle()
    result = runner.invoke(app, ["auth", "logout", "--yes"])
    assert result.exit_code == 0
    assert TokenStore(path=settings.spotify_token_path).load() is None


def test_logout_aborts_without_confirmation() -> None:
    settings = get_settings()
    _save_bundle()
    result = runner.invoke(app, ["auth", "logout"], input="n\n")
    assert result.exit_code != 0
    assert TokenStore(path=settings.spotify_token_path).load() is not None


# --- redirect URI 파싱 ----------------------------------------------------


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("http://127.0.0.1:8888/callback", ("127.0.0.1", 8888, "/callback")),
        ("http://127.0.0.1:9999/cb", ("127.0.0.1", 9999, "/cb")),
        ("http://localhost:8888/callback", ("localhost", 8888, "/callback")),
        # 포트 생략 시 기본값 — 등록 URI 와 어긋나면 Spotify 가 거부한다.
        ("http://127.0.0.1/callback", ("127.0.0.1", 8888, "/callback")),
    ],
)
def test_parse_redirect(uri: str, expected: tuple[str, int, str]) -> None:
    assert _parse_redirect(uri) == expected
