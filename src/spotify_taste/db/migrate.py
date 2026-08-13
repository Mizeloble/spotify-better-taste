"""무엇/왜
`PRAGMA user_version` 을 기준으로 하는 전진 전용 마이그레이션 러너.

설계 원칙
- **다운 마이그레이션 없음.** 단일 사용자 로컬 분석 DB 의 실제 복구 경로는
  "어젯밤 VACUUM INTO 스냅샷 복원 후 재실행"이다. 이건 다운 마이그레이션보다
  강하고, 야간 백업 잡이 매일 검증해 준다.
- 파일 하나 = 트랜잭션 하나. 적용과 `user_version` 상승이 같은 트랜잭션에서
  일어나므로, 중간에 죽어도 "절반만 적용된 버전" 이 남지 않는다.
- `verify()` 는 `sqlite_schema` 를 해시해 손편집을 잡는다. 스키마가 코드의
  가정과 어긋난 채로 수집이 도는 것이 가장 나쁜 상태다.

어떻게 확인
    uv run spotify-taste db migrate
    uv run pytest tests/test_db.py
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import structlog

from .._common import sha256_hex
from .connection import tx

__all__ = [
    "Migration",
    "current_version",
    "default_migrations_dir",
    "discover",
    "migrate",
    "schema_fingerprint",
]

log = structlog.get_logger(__name__)

_FILENAME_RE = re.compile(r"^(\d{4})_(\w+)\.sql$")

# 리포지토리에 동봉된 마이그레이션 디렉터리 (src/spotify_taste/db → 리포 루트).
# editable 설치에서는 이 경로가 맞는다. 아니면 cwd 로 떨어진다 (아래 참조).
_PACKAGE_RELATIVE_MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


def default_migrations_dir() -> Path:
    """마이그레이션 디렉터리를 찾는다.

    editable 설치(`uv sync` 의 기본)에서는 패키지 상대 경로가 리포 루트를
    가리킨다. 비-editable 설치를 대비해 작업 디렉터리도 본다 — launchd 래퍼가
    항상 리포 루트에서 실행하기 때문에 이 폴백이 실제로 동작한다.
    """
    if _PACKAGE_RELATIVE_MIGRATIONS.is_dir():
        return _PACKAGE_RELATIVE_MIGRATIONS
    return Path.cwd() / "migrations"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path

    def read_sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path | None = None) -> list[Migration]:
    """`NNNN_name.sql` 을 버전 오름차순으로 모은다.

    번호가 중복되면 어느 쪽이 적용됐는지 알 수 없게 되므로 즉시 실패한다.
    """
    base = directory or default_migrations_dir()
    if not base.is_dir():
        raise FileNotFoundError(f"마이그레이션 디렉터리가 없습니다: {base}")

    found: dict[int, Migration] = {}
    for path in sorted(base.glob("*.sql")):
        match = _FILENAME_RE.match(path.name)
        if not match:
            log.warning("db.migration_filename_ignored", path=str(path))
            continue
        version = int(match.group(1))
        if version in found:
            raise ValueError(
                f"마이그레이션 번호 중복: {version:04d} ({found[version].path.name}, {path.name})"
            )
        found[version] = Migration(version=version, name=match.group(2), path=path)
    return [found[v] for v in sorted(found)]


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[Migration]:
    """미적용 마이그레이션을 순서대로 적용하고 적용 목록을 돌려준다.

    이미 최신이면 아무것도 하지 않는다(멱등). launchd 래퍼가 매번 불러도 안전.
    """
    applied: list[Migration] = []
    for migration in discover(directory):
        version = current_version(conn)
        if migration.version <= version:
            continue
        if migration.version != version + 1:
            raise ValueError(
                f"마이그레이션 번호가 이어지지 않습니다: 현재 {version}, 다음 {migration.version}"
            )
        with tx(conn):
            # `tx()` 가 연 BEGIN IMMEDIATE 안에서 도므로 스크립트 중간에서
            # 실패해도 통째로 롤백된다 (test_db.py 에서 검증).
            # user_version 은 PRAGMA 라 바인딩 파라미터를 못 쓴다 — 값은 파일명에서
            # 온 정수라 주입 위험이 없다.
            conn.executescript(migration.read_sql())
            conn.execute(f"PRAGMA user_version = {migration.version}")
        applied.append(migration)
        log.info("db.migration_applied", version=migration.version, name=migration.name)
    return applied


def schema_fingerprint(conn: sqlite3.Connection) -> str:
    """스키마 정의 전체의 해시. 손편집 감지용.

    자동 생성 인덱스(`sqlite_autoindex_*`)는 sql 이 NULL 이라 자연히 빠진다.
    """
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_schema "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name"
    ).fetchall()
    payload = "\n".join(f"{r['type']}:{r['name']}:{r['sql']}" for r in rows)
    return sha256_hex(payload)
