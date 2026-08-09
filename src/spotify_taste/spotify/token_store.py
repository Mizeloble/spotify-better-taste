"""무엇/왜
토큰 번들의 디스크 저장. access token 은 잘려도 재발급이지만 **refresh token 은
손상되면 영구 손실**이고 사람이 브라우저 앞에 앉아야 복구된다. 그래서 형제
프로젝트의 토큰 캐시보다 두 가지가 더 있다: 원자적 쓰기와 직전 번들 백업.

설계 원칙
- **평문 JSON + 0600. 판단이지 게으름이 아니다.** macOS Keychain 을 기각한
  이유는, launchd 에이전트가 헤드리스에서 잠긴 키체인이나 ACL 프롬프트를 만나면
  *조용한 수집 사망*이 되기 때문이다 — dead-man switch 전체가 막으려는 바로 그
  실패 양상. 키를 파일 옆에 두고 암호화하는 건 보안 연극이다. 현실적 위협
  모델은 "커밋하지 마라, 출력하지 마라"이고, 이 토큰의 폭발 반경은
  *내 청취 기록 읽기 + 비공개 플레이리스트 생성*이다(돈도 신원도 없음).
- **원자적 쓰기**: 같은 디렉터리 temp → fsync → os.replace → chmod.
  os.replace 는 같은 파일시스템에서 원자적이라, 어떤 시점에 죽어도 파일은
  옛 번들이거나 새 번들이지 "절반"이 아니다.
- **`.bak` 보존**: Spotify 의 구 refresh token 은 회전 후에도 대개 유효하다.
  회전이 잘못됐을 때 실제로 되살릴 수 있는 경로다.
- **파손은 크래시가 아니라 재인증**이다. 읽다 실패하면 경고 로그 후 "없음"으로
  취급한다.

어떻게 확인
    uv run pytest tests/test_token_store.py
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .._common import mask_secret, now_ms

__all__ = ["TokenBundle", "TokenStore"]

log = structlog.get_logger(__name__)

# 번들 포맷 버전. 필드가 바뀌면 올리고, 모르는 버전은 읽지 않는다.
TOKEN_VERSION = 1

# 만료 몇 초 전부터 미리 갱신할지. 25초 폴러 한 틱보다 넉넉해야 한다.
REFRESH_MARGIN_MS = 120_000


@dataclass(frozen=True)
class TokenBundle:
    """디스크에 저장되는 토큰 묶음."""

    client_id: str
    access_token: str
    refresh_token: str
    expires_at_ms: int
    scopes: tuple[str, ...] = ()
    obtained_at_ms: int = 0
    token_version: int = TOKEN_VERSION

    def is_expired(self, *, now: int | None = None, margin_ms: int = REFRESH_MARGIN_MS) -> bool:
        """만료됐거나 곧 만료되는가."""
        return (now if now is not None else now_ms()) >= self.expires_at_ms - margin_ms

    def to_json(self) -> str:
        return json.dumps(
            {
                "client_id": self.client_id,
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at_ms": self.expires_at_ms,
                "scopes": list(self.scopes),
                "obtained_at_ms": self.obtained_at_ms,
                "token_version": self.token_version,
            },
            ensure_ascii=False,
            indent=2,
        )

    @classmethod
    def from_dict(cls, data: dict) -> TokenBundle:
        return cls(
            client_id=str(data["client_id"]),
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at_ms=int(data["expires_at_ms"]),
            scopes=tuple(data.get("scopes", ())),
            obtained_at_ms=int(data.get("obtained_at_ms", 0)),
            token_version=int(data.get("token_version", TOKEN_VERSION)),
        )

    def __repr__(self) -> str:
        # 번들을 통째로 로그에 찍어도 토큰이 새지 않게.
        return (
            f"TokenBundle(client_id={self.client_id!r}, "
            f"access={mask_secret(self.access_token)}, "
            f"refresh={mask_secret(self.refresh_token)}, "
            f"expires_at_ms={self.expires_at_ms})"
        )


@dataclass
class TokenStore:
    """토큰 번들을 읽고 쓴다. `path=None` 이면 메모리 전용(개발용)."""

    path: Path | None
    _memory: TokenBundle | None = field(default=None, repr=False)

    @property
    def backup_path(self) -> Path | None:
        return None if self.path is None else self.path.with_suffix(self.path.suffix + ".bak")

    def load(self, *, expected_client_id: str | None = None) -> TokenBundle | None:
        """저장된 번들. 없거나 못 읽으면 None (예외를 올리지 않는다).

        `expected_client_id` 를 주면 다른 앱의 토큰을 조용히 쓰는 일을 막는다 —
        Client ID 를 바꿨을 때 알 수 없는 403 대신 깔끔한 재인증이 되게.
        """
        if self.path is None:
            bundle = self._memory
        else:
            bundle = self._read_file(self.path)
            if bundle is None and self.backup_path is not None:
                # 본 파일이 깨졌으면 백업을 시도한다. 구 refresh token 은
                # 대개 유효하므로 이게 실제 복구 경로다.
                bundle = self._read_file(self.backup_path)
                if bundle is not None:
                    log.warning("auth.token_recovered_from_backup")

        if bundle is None:
            return None
        if bundle.token_version != TOKEN_VERSION:
            log.warning("auth.token_version_mismatch", found=bundle.token_version)
            return None
        if expected_client_id and bundle.client_id != expected_client_id:
            log.warning(
                "auth.token_client_id_changed",
                stored=bundle.client_id,
                configured=expected_client_id,
            )
            return None
        return bundle

    def save(self, bundle: TokenBundle) -> None:
        """번들을 원자적으로 저장한다."""
        if self.path is None:
            self._memory = bundle
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 덮어쓰기 전에 직전 번들을 보존한다.
        if self.path.exists() and self.backup_path is not None:
            try:
                shutil.copy2(self.path, self.backup_path)
                os.chmod(self.backup_path, 0o600)
            except OSError as exc:
                # 백업 실패가 저장을 막을 이유는 없다.
                log.warning("auth.token_backup_failed", error=str(exc))

        # 같은 디렉터리에 temp 를 만들어야 os.replace 가 원자적이다
        # (파일시스템을 넘으면 원자성이 깨진다).
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(bundle.to_json())
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        log.info(
            "auth.token_saved",
            path=str(self.path),
            expires_at_ms=bundle.expires_at_ms,
            scopes=len(bundle.scopes),
        )

    def invalidate(self) -> None:
        """번들과 백업을 함께 지운다. 재인증이 필요한 상태로 만든다."""
        self._memory = None
        if self.path is not None:
            self.path.unlink(missing_ok=True)
        if self.backup_path is not None:
            self.backup_path.unlink(missing_ok=True)
        log.info("auth.token_invalidated")

    def _read_file(self, path: Path) -> TokenBundle | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.warning("auth.token_read_failed", path=str(path), error=str(exc))
            return None
        try:
            return TokenBundle.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # 파손 → 크래시가 아니라 재인증. 수집기가 여기서 죽으면 안 된다.
            log.warning("auth.token_cache_corrupt", path=str(path), error=type(exc).__name__)
            return None
