"""출처 분류 결정표.

이 프로젝트 전체가 이 판정 위에 서 있다. 잘못 분류하면 Spotify 의 추천으로
학습해 놓고 '내 취향 모델'이라 부르게 된다.
"""

from __future__ import annotations

import pytest

from spotify_taste.classify.patterns import RULES_VERSION, matches_algorithmic_name
from spotify_taste.classify.rules import (
    MANUAL_START_SILENCE_MS,
    Evidence,
    Origin,
    classify,
)

ME = "jjmize"


def _ev(**over) -> Evidence:
    return Evidence(me_id=ME, **over)


# --- 플레이리스트 소유권 --------------------------------------------------


def test_my_own_playlist_is_self() -> None:
    origin, reason = classify(
        _ev(
            context_type="playlist",
            playlist_owner_id=ME,
            playlist_resolved=True,
            playlist_name="아무 이름",
        )
    )
    assert (origin, reason) == (Origin.SELF, "playlist_owner_me")


def test_spotify_owned_playlist_is_algorithmic() -> None:
    origin, _ = classify(
        _ev(context_type="playlist", playlist_owner_id="spotify", playlist_resolved=True)
    )
    assert origin is Origin.ALGORITHMIC


def test_someone_elses_playlist_is_external() -> None:
    origin, reason = classify(
        _ev(context_type="playlist", playlist_owner_id="친구계정", playlist_resolved=True)
    )
    assert (origin, reason) == (Origin.EXTERNAL, "playlist_owner_other")


def test_unresolved_owner_is_unknown_not_guessed() -> None:
    """추측하지 않는다 — 캐시가 채워지면 자동으로 치유된다."""
    origin, reason = classify(_ev(context_type="playlist", playlist_resolved=False))
    assert (origin, reason) == (Origin.UNKNOWN, "playlist_owner_unresolved")


def test_ownership_beats_name_pattern() -> None:
    """내가 만든 'Summer Mix' 는 self 다. 이름이 소유권을 이기면 안 된다."""
    origin, reason = classify(
        _ev(
            context_type="playlist",
            playlist_owner_id=ME,
            playlist_resolved=True,
            playlist_name="Chill Mix",
        )
    )
    assert (origin, reason) == (Origin.SELF, "playlist_owner_me")


def test_generated_flag_beats_ownership() -> None:
    """이게 없으면 Phase 4 산출물이 Phase 2 학습셋에 재유입돼 자기강화
    루프가 되고, 오염원이 곧 평가 데이터라 Phase 5 가 탐지할 수 없다."""
    origin, reason = classify(
        _ev(
            context_type="playlist",
            playlist_owner_id=ME,
            playlist_resolved=True,
            playlist_is_generated=True,
        )
    )
    assert (origin, reason) == (Origin.ALGORITHMIC, "own_generated_playlist")


def test_name_pattern_is_fallback_for_unresolved_owner() -> None:
    origin, reason = classify(
        _ev(context_type="playlist", playlist_name="Discover Weekly", playlist_resolved=False)
    )
    assert origin is Origin.ALGORITHMIC
    assert reason.startswith("playlist_name:")


# --- 다른 컨텍스트 --------------------------------------------------------


def test_radio_context_is_algorithmic() -> None:
    origin, reason = classify(_ev(context_uri="spotify:artist:X:radio:Y", context_type="artist"))
    assert (origin, reason) == (Origin.ALGORITHMIC, "radio_context")


def test_album_context_is_self() -> None:
    assert classify(_ev(context_type="album", context_uri="spotify:album:X"))[0] is Origin.SELF


def test_artist_context_is_self_but_flagged_as_assumption() -> None:
    """다음 rules 버전에서 재검토하라는 표시를 reason 에 남긴다."""
    origin, reason = classify(_ev(context_type="artist", context_uri="spotify:artist:X"))
    assert (origin, reason) == (Origin.SELF, "artist_context_assumed")


def test_liked_songs_is_self() -> None:
    origin, reason = classify(_ev(context_type="collection", context_uri="spotify:collection"))
    assert (origin, reason) == (Origin.SELF, "liked_songs_context")


def test_unhandled_context_type_is_unknown() -> None:
    origin, reason = classify(_ev(context_type="show", context_uri="spotify:show:X"))
    assert origin is Origin.UNKNOWN
    assert "unhandled_context_type" in reason


# --- null context (가장 위험한 구역) --------------------------------------


def test_null_context_defaults_to_unknown() -> None:
    """오토플레이도 null 로 온다. self 로 넣으면 Spotify 의 추천으로 학습해
    프로젝트가 자기 자신을 측정하게 된다 (C3)."""
    origin, reason = classify(_ev())
    assert (origin, reason) == (Origin.UNKNOWN, "null_ctx_no_evidence")


def test_null_context_with_library_hit_is_self() -> None:
    origin, reason = classify(_ev(track_in_library=True))
    assert (origin, reason) == (Origin.SELF, "null_ctx_library_hit")


def test_null_context_with_own_playlist_hit_is_self() -> None:
    origin, reason = classify(_ev(track_in_own_playlist=True))
    assert (origin, reason) == (Origin.SELF, "null_ctx_own_playlist")


def test_null_context_manual_start_is_self() -> None:
    origin, reason = classify(_ev(silence_before_ms=MANUAL_START_SILENCE_MS, shuffle=False))
    assert (origin, reason) == (Origin.SELF, "null_ctx_manual_start")


def test_manual_start_requires_shuffle_off() -> None:
    """셔플 중이면 사람이 그 곡을 고른 것이 아니다."""
    assert classify(_ev(silence_before_ms=600_000, shuffle=True))[0] is Origin.UNKNOWN


def test_short_silence_is_not_a_manual_start() -> None:
    """곡 사이 짧은 공백은 오토플레이와 구별되지 않는다."""
    assert classify(_ev(silence_before_ms=5_000, shuffle=False))[0] is Origin.UNKNOWN


# --- 성능저하 모드 --------------------------------------------------------


def test_degraded_mode_drops_the_silence_heuristic() -> None:
    """context 가 항상 null 인 계정에서는 대비가 없어 무음 휴리스틱이 무의미하다."""
    origin, reason = classify(_ev(silence_before_ms=600_000, shuffle=False, degraded_mode=True))
    assert (origin, reason) == (Origin.UNKNOWN, "null_ctx_degraded_no_evidence")


def test_degraded_mode_still_honours_library_membership() -> None:
    origin, _ = classify(_ev(track_in_library=True, degraded_mode=True))
    assert origin is Origin.SELF


# --- 이름 패턴 ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Discover Weekly",
        "새로운 발견",
        "Release Radar",
        "릴리스 레이더",
        "Daily Mix 3",
        "데일리 믹스 1",
        "On Repeat",
        "자주 듣는 음악",
        "Time Capsule",
        "타임캡슐",
        "Your Top Songs 2025",
        "Chill Mix",
        "혁오 믹스",
        "Wildflower Radio",
        "잔나비 라디오",
        "Your Blend",
        "Made For You",
        "Sonic Fingerprint",
        "Wrapped 2025",
    ],
)
def test_known_algorithmic_names_match(name: str) -> None:
    assert matches_algorithmic_name(name) is not None


@pytest.mark.parametrize(
    "name",
    ["출근길", "새벽에 듣는 것들", "2026 여름", "friend's picks", "OST 모음"],
)
def test_ordinary_playlist_names_do_not_match(name: str) -> None:
    """평범한 내 플레이리스트가 알고리즘으로 오분류되면 학습셋이 줄어든다."""
    assert matches_algorithmic_name(name) is None


def test_name_matching_is_case_insensitive() -> None:
    assert matches_algorithmic_name("discover weekly") is not None


def test_empty_name_does_not_match() -> None:
    assert matches_algorithmic_name(None) is None
    assert matches_algorithmic_name("") is None


# --- 버전 -----------------------------------------------------------------


def test_rules_version_is_a_positive_int() -> None:
    """세션 행이 자기를 분류한 버전을 들고 있어야 재계산이 성립한다."""
    assert isinstance(RULES_VERSION, int) and RULES_VERSION >= 1


def test_every_branch_returns_a_reason() -> None:
    """'왜 이렇게 분류됐지'에 답할 수 없으면 규칙을 고칠 수 없다."""
    cases = [
        _ev(),
        _ev(context_type="playlist"),
        _ev(context_type="album"),
        _ev(context_type="artist"),
        _ev(context_type="collection"),
        _ev(context_type="show"),
        _ev(track_in_library=True),
        _ev(context_uri="spotify:x:radio:y", context_type="artist"),
    ]
    for ev in cases:
        origin, reason = classify(ev)
        assert isinstance(origin, Origin)
        assert reason and isinstance(reason, str)


# --- 37i9dQZ 접두사 (v2) ---------------------------------------------------


def test_spotify_generated_prefix_is_algorithmic_when_owner_unresolvable() -> None:
    """2024-11 이후 Spotify 생성 플레이리스트는 /playlists/{id} 가 404 라
    owner 를 영영 알 수 없다 — 하필 algorithmic 으로 분류해야 할 대상만.
    실전 첫 데이터 50건이 전부 이걸로 unknown 이 됐던 사례에서 추가했다."""
    origin, reason = classify(
        _ev(
            context_type="playlist",
            context_uri="spotify:playlist:37i9dQZEVXcPpWDnhG5L1X",
            playlist_resolved=False,
        )
    )
    assert (origin, reason) == (Origin.ALGORITHMIC, "spotify_generated_uri_prefix")


def test_resolved_ownership_still_beats_the_prefix() -> None:
    """확인된 소유권은 접두사보다 강한 증거다."""
    origin, _ = classify(
        _ev(
            context_type="playlist",
            context_uri="spotify:playlist:37i9dQZWHATEVER",
            playlist_owner_id=ME,
            playlist_resolved=True,
        )
    )
    assert origin is Origin.SELF


def test_ordinary_unresolved_playlist_still_unknown() -> None:
    """접두사가 아니면 여전히 추측하지 않는다."""
    origin, _ = classify(
        _ev(
            context_type="playlist",
            context_uri="spotify:playlist:1bIrq9CRDeKEogJn3riyrp",
            playlist_resolved=False,
        )
    )
    assert origin is Origin.UNKNOWN
