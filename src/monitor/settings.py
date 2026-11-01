"""Runtime settings, read from the environment and a dotenv file.

Every tunable lives here with a default. Override any of them in `.env`
using `__` to reach nested groups, for example `LLM__DAILY_USD_CAP=40`.
`MONITOR_ENV_FILE` selects a different dotenv file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Paths(BaseModel):
    catalog: Path = Path("data/catalog.sqlite")
    data: Path = Path("data/lake/")
    models: Path = Path("data/models/")
    ui_db: Path = Path("data/ui.sqlite")
    backups: Path = Path("data/backups/")
    ref: Path = Path("data/ref/")
    control: Path = Path("data/control/")
    last_commit: Path = Path("data/last_commit.json")
    writer_lock: Path = Path("data/writer.lock")
    logs: Path = Path("logs/")
    reports: Path = Path("reports/")
    config: Path = Path("config/")

    @classmethod
    def under(cls, root: Path, config: Path | None = None) -> Paths:
        """Place all runtime paths under one directory."""
        return cls(
            catalog=root / "catalog.sqlite",
            data=root / "lake",
            models=root / "models",
            ui_db=root / "ui.sqlite",
            backups=root / "backups",
            ref=root / "ref",
            control=root / "control",
            last_commit=root / "last_commit.json",
            writer_lock=root / "writer.lock",
            logs=root / "logs",
            reports=root / "reports",
            config=config or Path("config/"),
        )


class Poll(BaseModel):
    tier_seconds: list[int] = [60, 180, 600]
    per_domain_concurrency: int = 2
    jitter_pct: int = 15
    timeout_seconds: float = 20.0
    user_agent: str = "ElectionNightMonitor/1.0 (personal research)"
    circuit_failures: int = 5
    circuit_retry_seconds: int = 900


class Commit(BaseModel):
    batch_seconds: int = 30
    maintenance_minutes: int = 15
    backup_minutes: int = 15
    backups_keep_recent: int = 12


class Lake(BaseModel):
    parquet_compression: str = "zstd"
    parquet_compression_level: int = 6
    target_file_size: str = "128MB"
    data_inlining_row_limit: int = 2000


class Nlp(BaseModel):
    relevance_threshold: float = 0.5
    embed_model: str = "intfloat/multilingual-e5-small"
    embed_batch_size: int = 32
    embed_duty_cycle: float = 0.4
    storyline_join: float = 0.82
    storyline_merge: float = 0.88
    storyline_promote_items: int = 3
    storyline_promote_sources: int = 2
    dedupe_cosine: float = 0.95
    dedupe_simhash_distance: int = 3
    dedupe_window_hours: int = 36
    entity_fuzzy_score: int = 92
    fetch_domain_interval_seconds: float = 3.0
    fetch_timeout_seconds: float = 10.0
    tour_seconds: int = 30


class Llm(BaseModel):
    model_bulk: str = "claude-haiku-5-5"
    model_brief: str = "claude-sonnet-5-5"
    concurrency_a: int = 6
    concurrency_b: int = 8
    concurrency_misc: int = 2
    batch_a: int = 10
    daily_usd_cap: float = 40.0
    hard_usd_cap: float = 75.0
    brief_minutes: int = 20
    brief_idle_minutes: int = 60
    brief_live_from: str = "2026-11-03T16:00:00-05:00"
    degrade_after_seconds: int = 120
    prices_per_mtok: dict[str, list[float]] = Field(
        default_factory=lambda: {
            "claude-haiku-5-5": [0.10, 0.50],
            "claude-sonnet-5-5": [2.0, 10.0],
        }
    )
    cache_read_multiplier: float = 0.1
    cache_write_multiplier: float = 1.25


class Web(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    fragment_throttle_seconds: int = 10
    snapshot_poll_seconds: float = 3.0
    query_cache_size: int = 512
    default_window: str = "1h"
    timezone: str = "America/New_York"


class Disk(BaseModel):
    warn_gb: float = 10.0
    degrade_gb: float = 5.0
    autoprune_gb: float = 3.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None
    fec_api_key: SecretStr | None = None
    paths: Paths = Field(default_factory=Paths)
    poll: Poll = Field(default_factory=Poll)
    commit: Commit = Field(default_factory=Commit)
    lake: Lake = Field(default_factory=Lake)
    nlp: Nlp = Field(default_factory=Nlp)
    llm: Llm = Field(default_factory=Llm)
    web: Web = Field(default_factory=Web)
    disk: Disk = Field(default_factory=Disk)


def load_settings(env_file: str | None = None) -> Settings:
    """Load the dotenv file into the process, then build settings.

    Libraries that read the environment directly, such as Hugging Face
    downloads, see the same values because the file is loaded first.
    """
    path = env_file or os.environ.get("MONITOR_ENV_FILE", ".env")
    load_dotenv(path, override=False)
    settings = Settings()
    models = str(settings.paths.models.resolve())
    os.environ.setdefault("HF_HOME", models)
    os.environ.setdefault("FASTEMBED_CACHE_PATH", models)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
