"""무엇/왜
프로젝트 전역에서 반복되는 순수 유틸 — 시간 변환, 시크릿 마스킹, 해시.
여기 있는 함수는 전부 부작용이 없고 I/O 를 하지 않는다. 그래야 어디서든
가져다 쓸 수 있고, import 순환이 생기지 않는다.

설계 원칙
- 시간은 **UTC epoch 밀리초 정수**가 단일 표준이다. ms 인 이유는 Spotify 가 주는
  모든 숫자(progress_ms, duration_ms)의 단위이고, 정수인 이유는 세션 병합의
  윈도우 산술이 부동소수 오차 없이 정확해야 하기 때문이다.
- KST 변환은 **표시 경계에서만** 한다. DB 에는 UTC ms 만 들어간다.
- 시크릿은 값이 아니라 마스킹된 형태로만 로그에 등장한다.

어떻게 확인
    uv run pytest tests/test_common.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

__all__ = [
    "KST",
    "epoch_ms_to_iso",
    "epoch_ms_to_kst",
    "iso_to_epoch_ms",
    "json_key_paths",
    "mask_secret",
    "now_ms",
    "sha256_hex",
    "shape_hash",
]

# 한국 표준시. DST 가 없어 고정 오프셋으로 안전하다 (play_session.local_date
# 생성 컬럼이 '+9 hours' 를 하드코딩할 수 있는 근거).
KST = timezone(timedelta(hours=9), name="KST")


def now_ms() -> int:
    """현재 시각을 UTC epoch 밀리초로. 테스트에서는 Clock 을 주입해 우회할 것."""
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def epoch_ms_to_iso(ms: int) -> str:
    """UTC epoch ms → ISO-8601 UTC 문자열 (로그·표시용)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def epoch_ms_to_kst(ms: int) -> datetime:
    """UTC epoch ms → KST aware datetime (사람이 읽는 리포트용)."""
    return datetime.fromtimestamp(ms / 1000, tz=KST)


def iso_to_epoch_ms(value: str) -> int:
    """ISO-8601 문자열 → UTC epoch ms.

    Spotify 의 `played_at`, `added_at` 은 'Z' 접미사를 쓰는데 3.11+ 의
    `fromisoformat` 이 이를 받아 주지만, 밀리초 자릿수가 들쭉날쭉해서
    직접 정규화한다.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def mask_secret(value: str | None, *, with_length: bool = True) -> str:
    """시크릿을 로그에 안전한 형태로. 앞 4글자만 남기고 길이를 덧붙인다."""
    if not value:
        return "<empty>"
    head = value[:4]
    return f"{head}…{len(value)}" if with_length else f"{head}…"


def sha256_hex(data: str | bytes) -> str:
    """문자열/바이트의 sha256 16진 다이제스트."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def json_key_paths(obj: Any, prefix: str = "") -> list[str]:
    """JSON 값에서 **정렬된 키 경로 목록**을 뽑는다.

    왜: Spotify API 가 필드를 계속 삭제하고 있어서, "응답의 모양이 바뀌었다"를
    자동으로 감지해야 한다. 키 경로 집합이 그 모양의 지문이다.

    설계:
    - 배열은 인덱스를 붙이지 않고 `[]` 로 접는다 → 항목 수 변화가 모양 변화로
      오인되지 않는다.
    - 값의 타입이나 내용은 보지 않는다 → 개인정보가 지문에 섞이지 않는다.
    - 결과를 정렬하므로 딕셔너리 키 순서에 독립적이다.
    """
    paths: set[str] = set()

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            if not node:
                paths.add(path or ".")
            for key in node:
                child = f"{path}.{key}" if path else key
                walk(node[key], child)
        elif isinstance(node, list):
            child = f"{path}[]"
            if not node:
                paths.add(child)
            for item in node:
                walk(item, child)
        else:
            paths.add(path or ".")

    walk(obj, prefix)
    return sorted(paths)


def shape_hash(obj: Any) -> tuple[str, str]:
    """JSON 값 → (shape_hash, key_paths_json).

    `api_schema_shape` 에 그대로 넣을 수 있는 쌍을 돌려준다. 처음 보는 해시가
    나오면 Telegram 알림이 나가고, 그게 필드 삭제를 실시간으로 잡는 장치다.
    """
    paths = json_key_paths(obj)
    payload = json.dumps(paths, ensure_ascii=False, separators=(",", ":"))
    return sha256_hex(payload), payload
