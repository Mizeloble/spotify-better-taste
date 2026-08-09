"""무엇/왜
테스트 안전망. 단위 테스트가 **운영 자산을 건드리지 못하게** 강제한다.

설계 원칙
- `_isolate_env` 는 autouse 다. 형제 프로젝트에서 단위 테스트가 운영 토큰 캐시를
  덮어써 다음 날 아침 실행을 죽인 사고가 있었다. 옵트인으로는 부족하다.
- 설정은 `@lru_cache` 라서 환경변수만 바꿔서는 반영되지 않는다 — 캐시를 비운다.

어떻게 확인
    uv run pytest
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from spotify_taste.config import get_settings
from spotify_taste.db.connection import connect
from spotify_taste.db.migrate import migrate


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """모든 파일 경로 설정을 tmp_path 로 돌리고 알림 채널을 끈다.

    운영 토큰 번들, 운영 DB, 운영 heartbeat 를 테스트가 만질 수 없게 한다.
    """
    monkeypatch.setenv("SPOTIFY_TOKEN_PATH", str(tmp_path / "auth" / "token.json"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("HEARTBEAT_PATH", str(tmp_path / "heartbeat" / "collector.txt"))
    monkeypatch.setenv("ALERT_DIR", str(tmp_path / "alerts"))
    # 알림은 기본적으로 끈다 — 테스트가 실제 Telegram 을 때리는 일이 없도록.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    # .env 파일이 있으면 그 값이 새어 들어오므로 읽지 않게 만든다.
    monkeypatch.chdir(tmp_path)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    """마이그레이션이 끝난 빈 DB 커넥션.

    `_isolate_env` 가 DB_PATH 를 tmp_path 로 돌려놓은 뒤라 운영 DB 를 건드릴
    수 없다.
    """
    conn = connect(get_settings().db_path)
    migrate(conn)
    try:
        yield conn
    finally:
        conn.close()
