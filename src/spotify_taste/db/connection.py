"""무엇/왜
SQLite 커넥션을 여는 단 하나의 경로. PRAGMA 설정과 트랜잭션 경계가 여기에만
있어야 "이 커넥션은 WAL 인가", "외래키가 켜져 있나" 를 매번 의심하지 않는다.

설계 원칙
- **stdlib sqlite3 만. ORM 없음.** 테이블이 ~20개로 거의 변하지 않고, 흥미로운
  쿼리는 전부 손으로 쓴 분석 SQL(윈도우 병합, KST 일자 그룹핑, upsert)이라
  ORM 이 방해만 된다. writer 가 하나뿐이라 세션/아이덴티티맵도 순수 오버헤드다.
  무엇보다 의존성 0 = 백그라운드 수집기의 트랜잭션 의미가 버전 업으로 조용히
  바뀔 가능성 0.
- `synchronous=NORMAL` 은 WAL 과 함께면 안전하다. 최악의 경우 잃는 것이
  25초 폴러 샘플 하나다.
- **쓰기 트랜잭션은 `BEGIN IMMEDIATE` 로 연다.** DEFERRED 로 시작하면
  읽기→쓰기 업그레이드 때 `SQLITE_BUSY_SNAPSHOT` 이 나는데, 이건 busy 핸들러가
  호출되지 않아 `busy_timeout` 을 통째로 무시하고 즉시 실패한다. `tx()` 가
  유일한 트랜잭션 경로인 이유가 이것이다.
- 트랜잭션은 짧게. 1틱 = 1트랜잭션. 트랜잭션 안에서 `await` 금지 —
  네트워크를 기다리는 동안 쓰기 락을 쥐고 있으면 폴러가 굶는다.

어떻게 확인
    uv run pytest tests/test_db.py
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import structlog

__all__ = ["connect", "tx"]

log = structlog.get_logger(__name__)

# 락 경합 시 즉시 포기하지 않고 5초 기다린다. 야간 백필과 폴러가 겹칠 때
# "database is locked" 로 샘플을 잃지 않게 하는 최소한의 여유.
BUSY_TIMEOUT_MS = 5_000


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """PRAGMA 가 적용된 커넥션을 연다.

    `readonly=True` 는 리포트/대시보드용. 실수로 쓰기가 일어나면 예외가 난다.
    """
    if readonly:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)

    conn.row_factory = sqlite3.Row
    # autocommit=True 를 유지하고 트랜잭션은 `tx()` 가 BEGIN IMMEDIATE 로 직접
    # 연다. 드라이버가 넣어 주는 암묵 BEGIN 은 DEFERRED 라서, 읽기로 시작한
    # 트랜잭션이 쓰기로 올라갈 때 busy_timeout 을 무시하고 즉시 죽는다
    # (2026-08-13 새벽 백필과 겹쳐 폴러가 통째로 종료된 사고의 원인).
    conn.autocommit = True

    cur = conn.cursor()
    if not readonly:
        # journal_mode 는 DB 파일에 영구 기록되므로 매번 설정해도 무해하다.
        cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """명시적 트랜잭션. 정상 종료면 커밋, 예외면 롤백 후 재전파.

    수집기의 한 틱이 통째로 성공하거나 통째로 실패하게 만드는 장치다.
    부분 기록된 샘플은 세션 재구성을 조용히 망가뜨린다.

    `BEGIN IMMEDIATE` 인 이유는 모듈 독스트링 참조 — 여기서 DEFERRED 를 쓰면
    락 경합이 `busy_timeout` 없이 즉시 예외가 된다.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
