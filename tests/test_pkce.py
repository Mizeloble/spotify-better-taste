"""PKCE 난수·챌린지·authorize URL."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from spotify_taste.config import SPOTIFY_SCOPES
from spotify_taste.spotify.pkce import (
    AUTHORIZE_URL,
    VERIFIER_LENGTH,
    authorize_url,
    new_challenge,
    state_matches,
)
from spotify_taste.spotify.pkce import _s256 as s256

# RFC 7636 부록 B 의 공식 테스트 벡터.
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

UNRESERVED = re.compile(r"^[A-Za-z0-9\-._~]+$")


def test_matches_rfc7636_test_vector() -> None:
    """구현이 규격과 같은 계산을 하는지 확인하는 유일한 객관적 근거."""
    assert s256(RFC_VERIFIER) == RFC_CHALLENGE


def test_verifier_length_is_rfc_maximum() -> None:
    assert len(new_challenge().verifier) == VERIFIER_LENGTH == 128


def test_verifier_charset_is_unreserved() -> None:
    """token_urlsafe 의 [A-Za-z0-9_-] 는 RFC unreserved 의 부분집합이다."""
    assert UNRESERVED.match(new_challenge().verifier)


def test_challenge_has_no_base64_padding() -> None:
    c = new_challenge()
    assert "=" not in c.challenge
    assert "+" not in c.challenge and "/" not in c.challenge


def test_challenge_derives_from_its_own_verifier() -> None:
    c = new_challenge()
    assert c.challenge == s256(c.verifier)


def test_method_is_s256_not_plain() -> None:
    assert new_challenge().method == "S256"


def test_each_call_produces_distinct_secrets() -> None:
    a, b = new_challenge(), new_challenge()
    assert a.verifier != b.verifier
    assert a.state != b.state
    assert a.challenge != b.challenge


# --- state 비교 -----------------------------------------------------------


def test_state_matches_exact() -> None:
    assert state_matches("abc", "abc") is True


def test_state_mismatch_rejected() -> None:
    assert state_matches("abc", "abd") is False


def test_missing_state_rejected() -> None:
    """state 없이 온 콜백은 토큰 교환 전에 막아야 한다."""
    assert state_matches("abc", None) is False
    assert state_matches("abc", "") is False


# --- authorize URL --------------------------------------------------------


def test_authorize_url_carries_every_required_param() -> None:
    c = new_challenge()
    url = authorize_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8888/callback",
        scopes=SPOTIFY_SCOPES,
        challenge=c,
    )
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZE_URL

    q = parse_qs(parsed.query)
    assert q["client_id"] == ["cid"]
    assert q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8888/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"] == [c.challenge]
    assert q["state"] == [c.state]


def test_authorize_url_never_contains_the_verifier() -> None:
    """verifier 가 URL 에 실리면 PKCE 의 의미가 통째로 사라진다."""
    c = new_challenge()
    url = authorize_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8888/callback",
        scopes=SPOTIFY_SCOPES,
        challenge=c,
    )
    assert c.verifier not in url


def test_scopes_are_space_delimited() -> None:
    c = new_challenge()
    url = authorize_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8888/callback",
        scopes=SPOTIFY_SCOPES,
        challenge=c,
    )
    assert parse_qs(urlparse(url).query)["scope"] == [" ".join(SPOTIFY_SCOPES)]


def test_show_dialog_can_be_disabled() -> None:
    c = new_challenge()
    url = authorize_url(
        client_id="cid",
        redirect_uri="http://127.0.0.1:8888/callback",
        scopes=SPOTIFY_SCOPES,
        challenge=c,
        show_dialog=False,
    )
    assert "show_dialog" not in parse_qs(urlparse(url).query)
