"""Server settings. The two secret tokens come ONLY from environment variables.

    GREENSETTLE_AGENT_TOKEN   the worker sends it in X-Agent-Token
    GREENSETTLE_ADMIN_TOKEN   the admin sends it in X-Admin-Token

The server refuses to start if either is missing or too short.
Generate one with:  python -c "import secrets; print(secrets.token_hex(32))"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from greensettle.intensity.timeline import ForecastErrorModel
from greensettle.ledger.config import DEFAULT_CONFIG, LedgerConfig
from greensettle.ledger.db import DEFAULT_DB_PATH
from greensettle.scheduler.dispatch import DEFAULT_MAX_CONCURRENT_JOBS
from greensettle.scheduler.queue import DEFAULT_MAX_QUEUED
from greensettle.scheduler.slots import SchedulerParams

AGENT_TOKEN_ENV = "GREENSETTLE_AGENT_TOKEN"
ADMIN_TOKEN_ENV = "GREENSETTLE_ADMIN_TOKEN"
MIN_TOKEN_LENGTH = 16


class ConfigError(RuntimeError):
    """The server cannot start with this configuration."""


@dataclass(frozen=True)
class ServerConfig:
    agent_token: str
    admin_token: str
    db_path: Path | str = DEFAULT_DB_PATH
    time_scale: float = 1.0  # sim seconds per real second
    sim_start: datetime | None = None  # first start only; default: real UTC now
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS  # PLACEHOLDER
    max_queued_per_user: int = DEFAULT_MAX_QUEUED  # PLACEHOLDER
    max_body_bytes: int = 16_384  # PLACEHOLDER: requests larger than this are refused (413)
    telemetry_keep: int = 1000  # rows of telemetry kept
    tick_interval_s: float = 1.0
    run_tick_thread: bool = True  # tests switch it off and call tick() themselves
    curve_seed: int = 42
    curve_tz: str = "UTC"  # time zone of the simulated curve's "hour of day"
    scheduler: SchedulerParams = field(default_factory=SchedulerParams)
    forecast_errors: ForecastErrorModel = field(default_factory=ForecastErrorModel)
    ledger: LedgerConfig = DEFAULT_CONFIG

    def __post_init__(self) -> None:
        for name in ("agent_token", "admin_token"):
            if len(getattr(self, name) or "") < MIN_TOKEN_LENGTH:
                raise ConfigError(f"{name} must be at least {MIN_TOKEN_LENGTH} characters")
        if self.agent_token == self.admin_token:
            raise ConfigError("agent and admin tokens must be different")
        if not self.time_scale > 0:
            raise ConfigError("time scale must be > 0")


def tokens_from_env() -> tuple[str, str]:
    agent, admin = os.environ.get(AGENT_TOKEN_ENV), os.environ.get(ADMIN_TOKEN_ENV)
    missing = [name for name, value in ((AGENT_TOKEN_ENV, agent), (ADMIN_TOKEN_ENV, admin)) if not value]
    if missing:
        raise ConfigError(
            f"refusing to start: environment variable(s) {', '.join(missing)} not set. "
            'Create tokens with: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return agent, admin  # type: ignore[return-value]
