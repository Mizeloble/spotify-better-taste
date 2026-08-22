"""셔플 시퀀스 컨텍스트 귀속 — 순수 함수.

지키는 불변식: **가장 가까운 과거 시퀀스가 이긴다**, **창 밖은 안 본다**,
**해석 못 하는 컨텍스트는 타입을 지어내지 않는다**, **셔플이 꺼진 재생의
증거는 low 다**.
"""

from __future__ import annotations

import pytest

from spotify_taste.importer.attribution import (
    ContextSequence,
    attribute,
    build_index,
    normalize_context_type,
)

T0 = 1_770_000_000_000
MIN = 60_000
PLAYLIST = "spotify:playlist:37i9dQZF1F5p3rmiWPIYgZ"
OTHER = "spotify:playlist:37i9dQZEVXcPpWDnhG5L1X"
A = "spotify:track:AAAA"
B = "spotify:track:BBBB"


def _index(*sequences: ContextSequence):
    return build_index(list(sequences))


def _seq(at: int, uri: str, *tracks: str, event_id: int | None = None) -> ContextSequence:
    return ContextSequence(at, uri, frozenset(tracks), event_id)


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("spotify:playlist:37i9dQZF1F5p3rmiWPIYgZ", "playlist"),
        ("spotify:user:31abc:collection", "collection"),
        ("spotify:user:31abc:collection:artist:4LLp", "collection"),
        ("spotify:list:popular-release-segments-main-roles:artist_5Vo1", "artist"),
        ("spotify:album:2Jxd", "album"),
        ("spotify:artist:1ikg", "artist"),
        ("spotify:track:1vPS", "single_track"),
        # 결정표가 모르는 종류. 지어내지 않는다 — 지어내면 self 가 unknown 으로
        # 퇴행한다 (정규화 없이 돌렸을 때 349건 발생).
        ("spotify:search:jazz", None),
        ("spotify:user:31abc", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_context_type(uri: str | None, expected: str | None) -> None:
    assert normalize_context_type(uri) == expected


def test_most_recent_sequence_wins() -> None:
    index = _index(_seq(T0, OTHER, A), _seq(T0 + MIN, PLAYLIST, A))
    found = attribute(A, T0 + 2 * MIN, index, shuffle=True)
    assert found is not None
    assert found.context_uri == PLAYLIST
    assert found.lag_ms == MIN


def test_falls_back_to_older_sequence_holding_the_track() -> None:
    """최신 시퀀스에 그 곡이 없으면 더 거슬러 올라간다."""
    index = _index(_seq(T0, PLAYLIST, A), _seq(T0 + MIN, OTHER, B))
    found = attribute(A, T0 + 2 * MIN, index, shuffle=True)
    assert found is not None and found.context_uri == PLAYLIST


def test_future_sequences_are_ignored() -> None:
    index = _index(_seq(T0 + 10 * MIN, PLAYLIST, A))
    assert attribute(A, T0, index, shuffle=True) is None


def test_window_cutoff() -> None:
    index = _index(_seq(T0, PLAYLIST, A))
    assert attribute(A, T0 + 5 * MIN, index, shuffle=True, window_ms=4 * MIN) is None
    assert attribute(A, T0 + 3 * MIN, index, shuffle=True, window_ms=4 * MIN) is not None


def test_unknown_track_is_not_attributed() -> None:
    index = _index(_seq(T0, PLAYLIST, A))
    assert attribute(B, T0 + MIN, index, shuffle=True) is None


def test_empty_index() -> None:
    assert attribute(A, T0, build_index([]), shuffle=True) is None


@pytest.mark.parametrize(
    ("lag_min", "shuffle", "expected"),
    [
        (1, True, "high"),
        (10, True, "high"),
        (11, True, "medium"),
        (60, True, "medium"),
        (61, True, "low"),
        # 시퀀스는 셔플이 켜진 세션에서만 발행된다. 꺼진 재생에 붙은 증거는
        # 낡았을 수 있어서 lag 가 짧아도 등급을 올리지 않는다 (223건).
        (1, False, "low"),
        (1, None, "low"),
    ],
)
def test_confidence_tiers(lag_min: int, shuffle: bool | None, expected: str) -> None:
    index = _index(_seq(T0, PLAYLIST, A))
    found = attribute(A, T0 + lag_min * MIN, index, shuffle=shuffle)
    assert found is not None and found.confidence == expected


def test_event_id_is_carried_through() -> None:
    index = _index(_seq(T0, PLAYLIST, A, event_id=42))
    found = attribute(A, T0 + MIN, index, shuffle=True)
    assert found is not None and found.event_id == 42


def test_build_index_sorts() -> None:
    index = build_index([_seq(T0 + MIN, OTHER, A), _seq(T0, PLAYLIST, A)])
    assert index.emitted_at == (T0, T0 + MIN)
