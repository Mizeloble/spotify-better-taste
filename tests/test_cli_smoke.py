"""모든 명령이 최소한 '사용법 오류' 없이 실행되는지.

이 파일이 있는 이유: `classify run --dry-run` 이 문서에는 있는데 코드에는
없어서 exit 2 로 떨어지는 버그를 손으로 발견했다. 문서와 플래그가 어긋나는
것은 테스트가 잡아야 한다.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from spotify_taste.cli import app

runner = CliRunner()

#: 네트워크가 필요 없는 명령들. exit 2(사용법 오류)가 나오면 실패다.
OFFLINE_COMMANDS = [
    ["db", "verify"],
    ["db", "stats"],
    ["db", "backup"],
    ["db", "prune", "--dry-run"],
    ["db", "integrity-check"],
    ["classify", "report"],
    ["backfill", "status"],
    ["sessions", "rebuild"],
    ["sessions", "rebuild", "--dry-run"],
    ["sessions", "rebuild", "--write"],
    ["ops", "status"],
    ["ops", "report"],
    ["ops", "heartbeat-check"],
    ["ops", "heartbeat-check", "--quiet"],
    ["auth", "status"],
    ["version"],
]


@pytest.mark.parametrize("argv", OFFLINE_COMMANDS, ids=lambda a: " ".join(a))
def test_command_is_not_a_usage_error(db: sqlite3.Connection, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code != 2, f"사용법 오류: {' '.join(argv)}\n{result.output}"


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["auth", "--help"],
        ["collect", "--help"],
        ["classify", "--help"],
        ["classify", "run", "--help"],
        ["sessions", "--help"],
        ["backfill", "--help"],
        ["snapshot", "--help"],
        ["db", "--help"],
        ["ops", "--help"],
    ],
    ids=lambda a: " ".join(a),
)
def test_help_renders(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0


def test_documented_dry_run_flags_exist(db: sqlite3.Connection) -> None:
    """문서와 CLAUDE.md 가 약속하는 dry-run 형태가 실제로 동작해야 한다."""
    for argv in (
        ["classify", "run", "--dry-run"],
        ["classify", "run", "--write"],
        ["sessions", "rebuild", "--dry-run"],
        ["sessions", "rebuild", "--write"],
        ["db", "prune", "--dry-run"],
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code != 2, f"{' '.join(argv)} 가 사용법 오류입니다"
