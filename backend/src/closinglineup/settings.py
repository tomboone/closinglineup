"""All runtime configuration, read from the environment (spec §10)."""

from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# (|margin| ≥ M, ≤ T seconds left in the fourth quarter) pairs (spec §7.4); `derive.garbage_time`
# applies them.
type GarbageTimeThresholds = tuple[tuple[int, int], ...]

# A fourth quarter's length, the most seconds a garbage-time threshold can leave.
QUARTER_SECONDS = 720


def _split_pairs(value: object) -> object:
    """`"25:540,20:360"` → `[["25", "540"], ["20", "360"]]`; pydantic coerces and checks it."""
    if isinstance(value, str):
        return [item.strip().split(":") for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    # Required; `default=...` only tells pyright that env, not __init__, supplies it.
    database_url: str = Field(default=...)
    data_dir: Path = Path("/data")
    tz: ZoneInfo = ZoneInfo("America/New_York")

    nba_rps: float = Field(default=1.0, gt=0)
    nba_timeout_seconds: float = Field(default=30, gt=0)
    # The days before its date whose games' missing endpoints `nightly` fetches again.
    nightly_lookback_days: int = Field(default=3, ge=0)

    # How long `worker` waits for `app` to migrate the schema to head before it exits (non-zero,
    # for the restart policy to start it again).
    schema_wait_timeout_seconds: float = Field(default=300, gt=0)

    healthchecks_url_nightly: str | None = None
    healthchecks_url_daily: str | None = None
    healthchecks_url_weekly: str | None = None

    # Off until each source's terms of use are checked (spec §2).
    epm_enabled: bool = False
    darko_enabled: bool = False
    hoopsrumors_enabled: bool = False

    garbage_time_rule: Annotated[GarbageTimeThresholds, NoDecode] = Field(
        default=((25, 540), (20, 360), (15, 180), (10, 60)), min_length=1
    )
    # Started at least N of the last K games played, from `"N/K"` (spec §7.5).
    tier_starter_min: Annotated[tuple[int, int], NoDecode] = (4, 5)
    # A share of the team's player-minutes (§7.1): 0.036 is 18% of one player's 48, 8.6 a game.
    tier_core_share: float = Field(default=0.036, gt=0, le=1)
    tier_fringe_played_rate: float = Field(default=0.5, ge=0, le=1)

    log_level: LogLevel = "INFO"
    # Unset means the season is derived from today; set it to backfill an earlier one.
    season: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")

    @field_validator("garbage_time_rule", mode="before")
    @classmethod
    def _parse_garbage_time_rule(cls, value: object) -> object:
        return _split_pairs(value)

    @field_validator("garbage_time_rule")
    @classmethod
    def _check_garbage_time_rule(cls, value: GarbageTimeThresholds) -> GarbageTimeThresholds:
        for margin, seconds in value:
            if margin < 1 or not 0 < seconds <= QUARTER_SECONDS:
                raise ValueError(
                    f"each pair must be M:T with M ≥ 1 points and 0 < T ≤ {QUARTER_SECONDS} "
                    f"seconds left, not {margin}:{seconds}"
                )
        return value

    @field_validator("tier_starter_min", mode="before")
    @classmethod
    def _parse_tier_starter_min(cls, value: object) -> object:
        return value.strip().split("/") if isinstance(value, str) else value

    @field_validator("tier_starter_min")
    @classmethod
    def _check_tier_starter_min(cls, value: tuple[int, int]) -> tuple[int, int]:
        started, of = value
        if not 1 <= started <= of:
            raise ValueError("must be N/K with 1 ≤ N ≤ K")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value
