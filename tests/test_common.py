"""시간 변환, 마스킹, 응답 모양 지문."""

from __future__ import annotations

from spotify_taste._common import (
    epoch_ms_to_iso,
    epoch_ms_to_kst,
    iso_to_epoch_ms,
    json_key_paths,
    mask_secret,
    shape_hash,
)


def test_iso_roundtrip_utc() -> None:
    ms = 1_754_000_000_000
    assert iso_to_epoch_ms(epoch_ms_to_iso(ms)) == ms


def test_spotify_z_suffix_parsed() -> None:
    """`played_at` 은 'Z' 접미사로 온다."""
    assert iso_to_epoch_ms("2026-08-09T12:00:00Z") == 1_786_276_800_000


def test_fractional_seconds_parsed() -> None:
    a = iso_to_epoch_ms("2026-08-09T12:00:00.123Z")
    b = iso_to_epoch_ms("2026-08-09T12:00:00Z")
    assert a - b == 123


def test_naive_timestamp_assumed_utc() -> None:
    assert iso_to_epoch_ms("2026-08-09T12:00:00") == iso_to_epoch_ms("2026-08-09T12:00:00Z")


def test_kst_is_utc_plus_nine() -> None:
    dt = epoch_ms_to_kst(iso_to_epoch_ms("2026-08-09T12:00:00Z"))
    assert (dt.hour, dt.day) == (21, 9)


def test_kst_crosses_date_boundary() -> None:
    """UTC 16시는 KST 로 다음 날 새벽 1시 — local_date 그룹핑의 근거."""
    dt = epoch_ms_to_kst(iso_to_epoch_ms("2026-08-09T16:00:00Z"))
    assert (dt.hour, dt.day) == (1, 10)


def test_mask_secret_keeps_only_prefix_and_length() -> None:
    masked = mask_secret("abcdefghij")
    assert masked == "abcd…10"
    assert "efghij" not in masked


def test_mask_secret_on_empty() -> None:
    assert mask_secret("") == "<empty>"
    assert mask_secret(None) == "<empty>"


def test_key_paths_ignore_dict_order() -> None:
    assert json_key_paths({"a": 1, "b": 2}) == json_key_paths({"b": 2, "a": 1})


def test_key_paths_collapse_array_indices() -> None:
    """항목이 1개든 3개든 같은 모양이어야 한다."""
    one = json_key_paths({"items": [{"id": 1}]})
    three = json_key_paths({"items": [{"id": 1}, {"id": 2}, {"id": 3}]})
    assert one == three == ["items[].id"]


def test_key_paths_ignore_values() -> None:
    """값은 지문에 섞이지 않는다 — 개인정보가 해시에 들어가면 안 된다."""
    assert json_key_paths({"name": "내 플레이리스트"}) == json_key_paths({"name": "x"})


def test_shape_hash_changes_when_field_removed() -> None:
    """이게 이 프로젝트에서 필드 삭제를 실시간으로 잡는 장치의 핵심이다."""
    before, _ = shape_hash({"id": "x", "popularity": 50, "name": "n"})
    after, _ = shape_hash({"id": "x", "name": "n"})
    assert before != after


def test_shape_hash_stable_across_equivalent_payloads() -> None:
    a, _ = shape_hash({"track": {"id": "1"}, "items": [{"x": 1}]})
    b, _ = shape_hash({"items": [{"x": 9}, {"x": 8}], "track": {"id": "2"}})
    assert a == b


def test_shape_hash_returns_serialized_paths() -> None:
    _, paths_json = shape_hash({"a": {"b": 1}})
    assert paths_json == '["a.b"]'


def test_nested_null_is_a_leaf_not_a_missing_key() -> None:
    """null 인 context 도 키 자체는 존재한다 — 삭제와 구분돼야 한다."""
    with_null, _ = shape_hash({"context": None})
    without, _ = shape_hash({})
    assert with_null != without
