"""무엇/왜
Authorization Code with PKCE 의 순수 계산 부분. Implicit Grant 는 폐지됐고,
PKCE 는 client secret 없이 공개 클라이언트가 안전하게 코드를 교환하는 방법이다.

설계 원칙
- I/O 가 전혀 없다. 난수 생성만 하고 네트워크도 디스크도 만지지 않는다 —
  RFC 7636 부록 B 의 테스트 벡터로 직접 검증할 수 있게.
- verifier 와 state 는 **교환이 성공할 때까지 프로세스 메모리에만** 존재한다.
  디스크에 임시 저장하지 않는다.
- state 비교는 `secrets.compare_digest`. 타이밍 공격 자체는 로컬 루프백에서
  현실적 위협이 아니지만, 비교를 틀리게 쓰는 습관이 남는 편이 더 위험하다.

어떻게 확인
    uv run pytest tests/test_pkce.py
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

__all__ = ["AUTHORIZE_URL", "PkceChallenge", "authorize_url", "new_challenge", "state_matches"]

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"

# RFC 7636 이 정한 code_verifier 길이 범위. 최대치를 쓴다.
VERIFIER_LENGTH = 128


@dataclass(frozen=True)
class PkceChallenge:
    """한 번의 인증 시도에 쓰이는 난수 묶음."""

    verifier: str
    challenge: str
    state: str
    method: str = "S256"


def new_challenge() -> PkceChallenge:
    """새 verifier/challenge/state 를 만든다.

    `token_urlsafe` 의 출력 문자셋 `[A-Za-z0-9_-]` 는 RFC 7636 의 unreserved
    집합 `[A-Za-z0-9\\-._~]` 의 부분집합이라 그대로 써도 규격을 만족한다.
    96바이트를 요청하면 128자보다 길게 나오므로 잘라 쓴다.
    """
    verifier = secrets.token_urlsafe(96)[:VERIFIER_LENGTH]
    return PkceChallenge(
        verifier=verifier,
        challenge=_s256(verifier),
        state=secrets.token_urlsafe(32),
    )


def _s256(verifier: str) -> str:
    """BASE64URL(SHA256(ASCII(verifier))) — 패딩 제거."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    challenge: PkceChallenge,
    show_dialog: bool = True,
) -> str:
    """사용자를 보낼 authorize URL.

    `show_dialog=true` 로 두는 이유: 이미 승인된 상태에서 조용히 리다이렉트되면
    "어떤 scope 를 줬는지" 를 사람이 확인할 기회가 사라진다. 재인증은 드문
    이벤트라 한 번 더 보여 주는 비용이 싸다.
    """
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge_method": challenge.method,
        "code_challenge": challenge.challenge,
        "state": challenge.state,
        "scope": " ".join(scopes),
    }
    if show_dialog:
        params["show_dialog"] = "true"
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def state_matches(expected: str, received: str | None) -> bool:
    """CSRF 방어. 불일치면 **토큰 교환 전에** 중단해야 한다."""
    if not received:
        return False
    return secrets.compare_digest(expected, received)
