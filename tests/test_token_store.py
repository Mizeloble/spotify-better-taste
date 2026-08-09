"""토큰 번들 저장 — 원자성, 권한, 파손 내성.

refresh token 이 손상되면 사람이 브라우저 앞에 앉아야 복구된다. 이 파일이
지키는 건 "어떤 시점에 죽어도 파일은 옛 번들이거나 새 번들이지 절반이 아니다".
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from spotify_taste.spotify.token_store import (
    REFRESH_MARGIN_MS,
    TOKEN_VERSION,
    TokenBundle,
    TokenStore,
)


def _bundle(**over) -> TokenBundle:
    data = {
        "client_id": "cid",
        "access_token": "ACCESS-SECRET",
        "refresh_token": "REFRESH-SECRET",
        "expires_at_ms": 2_000_000_000_000,
        "scopes": ("user-read-playback-state",),
        "obtained_at_ms": 1_000_000_000_000,
    }
    data.update(over)
    return TokenBundle(**data)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(path=tmp_path / "auth" / "token.json")


# --- 저장/로드 왕복 -------------------------------------------------------


def test_roundtrip(store: TokenStore) -> None:
    original = _bundle()
    store.save(original)
    assert store.load() == original


def test_creates_parent_directory(tmp_path: Path) -> None:
    store = TokenStore(path=tmp_path / "deep" / "nested" / "token.json")
    store.save(_bundle())
    assert store.load() is not None


def test_load_missing_returns_none(store: TokenStore) -> None:
    assert store.load() is None


def test_file_is_owner_only_readable(store: TokenStore) -> None:
    """0600 이 아니면 같은 머신의 다른 사용자가 refresh token 을 읽는다."""
    store.save(_bundle())
    mode = stat.S_IMODE(store.path.stat().st_mode)  # type: ignore[union-attr]
    assert mode == 0o600


def test_no_temp_files_left_behind(store: TokenStore) -> None:
    store.save(_bundle())
    leftovers = list(store.path.parent.glob(".token-*.tmp"))  # type: ignore[union-attr]
    assert leftovers == []


def test_temp_written_in_same_directory(tmp_path: Path, monkeypatch) -> None:
    """파일시스템을 넘으면 os.replace 의 원자성이 깨진다."""
    seen: list[str] = []
    import tempfile as tempfile_mod

    real_mkstemp = tempfile_mod.mkstemp

    def spy(*args, **kwargs):
        seen.append(str(kwargs.get("dir")))
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("spotify_taste.spotify.token_store.tempfile.mkstemp", spy)
    store = TokenStore(path=tmp_path / "auth" / "token.json")
    store.save(_bundle())
    assert seen == [str(tmp_path / "auth")]


# --- 백업 -----------------------------------------------------------------


def test_backup_written_on_overwrite(store: TokenStore) -> None:
    store.save(_bundle(refresh_token="OLD"))
    store.save(_bundle(refresh_token="NEW"))
    backup = json.loads(store.backup_path.read_text())  # type: ignore[union-attr]
    assert backup["refresh_token"] == "OLD"
    assert store.load().refresh_token == "NEW"  # type: ignore[union-attr]


def test_no_backup_on_first_save(store: TokenStore) -> None:
    store.save(_bundle())
    assert not store.backup_path.exists()  # type: ignore[union-attr]


def test_backup_is_owner_only(store: TokenStore) -> None:
    store.save(_bundle())
    store.save(_bundle(access_token="A2"))
    mode = stat.S_IMODE(store.backup_path.stat().st_mode)  # type: ignore[union-attr]
    assert mode == 0o600


def test_corrupt_primary_recovers_from_backup(store: TokenStore) -> None:
    """구 refresh token 은 회전 후에도 대개 유효하다 — 실제 복구 경로."""
    store.save(_bundle(refresh_token="GOOD-OLD"))
    store.save(_bundle(refresh_token="GOOD-NEW"))
    store.path.write_text("{절반만 쓰이다 죽음")  # type: ignore[union-attr]
    recovered = store.load()
    assert recovered is not None
    assert recovered.refresh_token == "GOOD-OLD"


# --- 파손 내성 ------------------------------------------------------------


def test_corrupt_json_treated_as_absent(store: TokenStore) -> None:
    """수집기가 파손된 캐시에서 죽으면 안 된다."""
    store.path.parent.mkdir(parents=True)  # type: ignore[union-attr]
    store.path.write_text("not json at all")  # type: ignore[union-attr]
    assert store.load() is None


def test_missing_required_field_treated_as_absent(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)  # type: ignore[union-attr]
    store.path.write_text(json.dumps({"client_id": "cid"}))  # type: ignore[union-attr]
    assert store.load() is None


def test_empty_file_treated_as_absent(store: TokenStore) -> None:
    store.path.parent.mkdir(parents=True)  # type: ignore[union-attr]
    store.path.write_text("")  # type: ignore[union-attr]
    assert store.load() is None


def test_unknown_token_version_rejected(store: TokenStore) -> None:
    store.save(_bundle())
    data = json.loads(store.path.read_text())  # type: ignore[union-attr]
    data["token_version"] = TOKEN_VERSION + 99
    store.path.write_text(json.dumps(data))  # type: ignore[union-attr]
    assert store.load() is None


# --- client_id 변경 -------------------------------------------------------


def test_client_id_change_invalidates_cache(store: TokenStore) -> None:
    """Client ID 를 바꿨을 때 알 수 없는 403 대신 깔끔한 재인증이 되게."""
    store.save(_bundle(client_id="old-app"))
    assert store.load(expected_client_id="old-app") is not None
    assert store.load(expected_client_id="new-app") is None


def test_client_id_check_is_optional(store: TokenStore) -> None:
    store.save(_bundle(client_id="whatever"))
    assert store.load() is not None


# --- invalidate -----------------------------------------------------------


def test_invalidate_removes_both_files(store: TokenStore) -> None:
    store.save(_bundle())
    store.save(_bundle(access_token="A2"))
    assert store.path.exists() and store.backup_path.exists()  # type: ignore[union-attr]
    store.invalidate()
    assert not store.path.exists()  # type: ignore[union-attr]
    assert not store.backup_path.exists()  # type: ignore[union-attr]


def test_invalidate_on_missing_files_is_safe(store: TokenStore) -> None:
    store.invalidate()  # 예외가 나면 안 된다


# --- 메모리 전용 모드 -----------------------------------------------------


def test_memory_only_store_roundtrip() -> None:
    store = TokenStore(path=None)
    assert store.load() is None
    store.save(_bundle())
    assert store.load().access_token == "ACCESS-SECRET"  # type: ignore[union-attr]
    store.invalidate()
    assert store.load() is None


def test_memory_only_writes_nothing_to_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    TokenStore(path=None).save(_bundle())
    assert list(tmp_path.iterdir()) == []


# --- 만료 판정 ------------------------------------------------------------


def test_not_expired_well_before_deadline() -> None:
    b = _bundle(expires_at_ms=1_000_000)
    assert b.is_expired(now=0) is False


def test_expired_after_deadline() -> None:
    b = _bundle(expires_at_ms=1_000_000)
    assert b.is_expired(now=1_000_001) is True


def test_refresh_margin_triggers_early() -> None:
    """만료 직전에 갱신해야 폴러 한 틱이 401 로 낭비되지 않는다."""
    b = _bundle(expires_at_ms=1_000_000)
    assert b.is_expired(now=1_000_000 - REFRESH_MARGIN_MS + 1) is True
    assert b.is_expired(now=1_000_000 - REFRESH_MARGIN_MS - 1) is False


# --- 시크릿 유출 방지 -----------------------------------------------------


def test_repr_masks_tokens() -> None:
    text = repr(_bundle())
    assert "ACCESS-SECRET" not in text
    assert "REFRESH-SECRET" not in text
    assert "cid" in text  # client_id 는 공개 식별자라 그대로 둔다


def test_store_repr_does_not_contain_bundle() -> None:
    store = TokenStore(path=None)
    store.save(_bundle())
    assert "REFRESH-SECRET" not in repr(store)


def test_saved_file_is_valid_json_with_expected_keys(store: TokenStore) -> None:
    store.save(_bundle())
    data = json.loads(store.path.read_text())  # type: ignore[union-attr]
    assert set(data) == {
        "client_id",
        "access_token",
        "refresh_token",
        "expires_at_ms",
        "scopes",
        "obtained_at_ms",
        "token_version",
    }


def test_save_failure_leaves_previous_bundle_intact(store: TokenStore, monkeypatch) -> None:
    """새 저장이 깨져도 옛 번들은 살아 있어야 한다."""
    store.save(_bundle(refresh_token="ORIGINAL"))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.save(_bundle(refresh_token="NEW"))

    assert store.load().refresh_token == "ORIGINAL"  # type: ignore[union-attr]
    assert list(store.path.parent.glob(".token-*.tmp")) == []  # type: ignore[union-attr]
