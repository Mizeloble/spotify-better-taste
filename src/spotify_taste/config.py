"""무엇/왜
`.env` 하나에서 모든 설정을 읽어 들이는 단일 진입점. 형제 프로젝트
(auto-stock-trading)의 규약을 그대로 따른다 — `Settings` 클래스 하나,
`@lru_cache get_settings()` 하나, 그 밖의 설정 소스는 없다.

설계 원칙
- 시크릿은 `SecretStr`. 단, **Spotify Client ID 는 시크릿이 아니다** — PKCE 는
  client secret 을 쓰지 않고 client_id 는 사용자가 보는 authorize URL 에 그대로
  노출되는 공개 식별자다. 시크릿처럼 다루면 오히려 로그 디버깅만 어려워진다.
- 선택 기능의 경로는 `Path | None` 이고 **None = 기능 비활성**이다
  (`backup_dir`, `spotify_token_path`).
- "설정이 켜져 있는가"는 호출부에서 인라인으로 판단하지 않고 `has_*` 프로퍼티로
  묻는다. 판단 기준이 한 곳에만 있어야 나중에 바뀌어도 안전하다.
- `env_ignore_empty=True`: 빈 문자열로 export 된 환경변수는 무시하고 `.env` 값을
  쓴다. launchd 가 빈 환경을 주는 상황에서 실제로 필요했다.

어떻게 확인
    uv run pytest tests/test_config.py
    uv run spotify-taste --help
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["SPOTIFY_SCOPES", "Settings", "get_settings"]

# Phase 1~6 에서 실제로 쓰는 scope 전부. 늘어나면 재인증이 필요하므로
# `auth status` 가 현재 토큰의 scope 와 이 목록을 비교해 경고한다.
SPOTIFY_SCOPES: tuple[str, ...] = (
    # /me 의 `product` 필드는 이 scope 가 있어야만 응답에 포함된다.
    # 없으면 Premium 계정에서도 null 이 와서 preflight 가 오판한다 —
    # 실제로 겪고 나서 추가했다.
    "user-read-private",
    "user-read-playback-state",
    "user-read-recently-played",
    "user-top-read",
    "user-library-read",
    "playlist-read-private",
    "playlist-modify-private",
)


class Settings(BaseSettings):
    """`.env` + 환경변수에서 읽는 전역 설정."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # --- Spotify (PKCE — client secret 없음, 의도된 설계) ---
    spotify_client_id: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8888/callback"
    # None => 메모리 전용(개발). 운영에서는 반드시 파일 경로를 준다.
    spotify_token_path: Path | None = Path("data/auth/spotify-token.json")

    # --- 저장소 ---
    db_path: Path = Path("data/spotify-taste.db")
    backup_dir: Path | None = Path("data/backups")
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 8
    # /me/player 원본을 N 틱마다 1회 통째로 보관 (2계층 아카이브의 카나리).
    raw_player_sample_every: int = 20

    # --- 폴러 튜닝 ---
    player_poll_sec: int = 25
    # 연속 유휴(204) 시 늘려 갈 간격. 상한 120초 — 300초로 두면 청취 재개 후
    # 최대 5분을 ms_played 없는 recently-played 경로에 넘기게 된다.
    player_idle_backoff_sec: str = "25,45,90,120"
    recent_poll_sec: int = 600
    backfill_min_interval_sec: float = 0.5
    backfill_max_calls_per_run: int = 3000
    # 이보다 긴 Retry-After 는 자지 않고 배치를 중단한다. 수집기 안에서 한 시간
    # 자는 것은 행(hang)과 구별되지 않는다.
    max_retry_after_sec: int = 300

    # --- 분류 ---
    # true => context 가 항상 null 로 오는 계정을 위한 성능저하 모드.
    # RULES_VERSION 의 일부이므로 뒤집으면 전 행이 stale 이 된다.
    context_null_account: bool = False

    # --- 운영 ---
    log_level: str = "INFO"
    heartbeat_path: Path = Path("data/heartbeat/collector.txt")
    # 알림 종류별 쿨다운 파일이 놓이는 디렉터리 (한 알림이 다른 알림을 삼키지
    # 않게 kind 마다 파일 1개).
    alert_dir: Path = Path("data/alerts")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    @field_validator("player_idle_backoff_sec")
    @classmethod
    def _validate_backoff(cls, value: str) -> str:
        """콤마 구분 양의 정수 목록인지 로드 시점에 검증한다.

        폴러가 새벽 3시에 ValueError 로 죽는 것보다 기동 시 실패가 낫다.
        """
        steps = _parse_int_csv(value)
        if not steps:
            raise ValueError("player_idle_backoff_sec 는 최소 1개의 정수가 필요합니다")
        if any(step <= 0 for step in steps):
            raise ValueError("player_idle_backoff_sec 의 모든 값은 양수여야 합니다")
        return value

    # --- 파생 값 ---

    @property
    def idle_backoff_steps(self) -> list[int]:
        """유휴 백오프 단계(초). 마지막 값이 상한으로 계속 쓰인다."""
        return _parse_int_csv(self.player_idle_backoff_sec)

    @property
    def has_telegram(self) -> bool:
        """알림 채널이 설정돼 있는가. 아니면 NoopNotifier 가 쓰인다."""
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    @property
    def has_backup(self) -> bool:
        return self.backup_dir is not None

    @property
    def has_token_file(self) -> bool:
        """토큰을 디스크에 보관하는가. False 면 프로세스 종료 시 재인증 필요."""
        return self.spotify_token_path is not None

    @property
    def auth_dir(self) -> Path:
        """토큰 번들과 재인증 센티넬이 놓이는 디렉터리."""
        if self.spotify_token_path is not None:
            return self.spotify_token_path.parent
        return Path("data/auth")

    @property
    def reauth_sentinel_path(self) -> Path:
        """refresh token 이 영구 폐기됐을 때 남기는 표식.

        launchd 래퍼가 이 파일을 보고 exit 0 으로 빠져나가 재시작 폭풍을 막는다.
        """
        return self.auth_dir / "reauth_required"

    @property
    def shutdown_sentinel_path(self) -> Path:
        """정상 종료 표식. 있으면 heartbeat-check 가 알림을 내지 않는다."""
        return self.heartbeat_path.parent / "stopped.txt"

    @property
    def scopes(self) -> tuple[str, ...]:
        return SPOTIFY_SCOPES


def _parse_int_csv(value: str) -> list[int]:
    """ "25,45,90" → [25, 45, 90]. 빈 조각은 무시한다.

    pydantic-settings 는 `list[int]` 필드를 환경변수에서 읽을 때 JSON 파싱을
    시도해 "25,45,90" 에서 실패한다. 그래서 str 로 받고 여기서 푼다.
    """
    out: list[int] = []
    for chunk in value.split(","):
        text = chunk.strip()
        if not text:
            continue
        out.append(int(text))
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """프로세스 전역 설정. 테스트는 `get_settings.cache_clear()` 로 초기화한다."""
    return Settings()
