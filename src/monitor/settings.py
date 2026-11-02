"""Runtime settings, read from the environment and a dotenv file.

Every tunable lives here with a default. Override any of them in `.env`
using `__` to reach nested groups, for example `LLM__DAILY_USD_CAP=40`.
`MONITOR_ENV_FILE` selects a different dotenv file.
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

from dotenv import load_dotenv
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# These locate one lake and its writer, so they move together. A command that
# moved only the catalog would still see the live writer lock, and would send
# its request to the live pipeline.
LAKE_PATHS = (
    "catalog",
    "data",
    "backups",
    "control",
    "last_commit",
    "status",
    "archive",
    "writer_lock",
)


class Paths(BaseModel):
    catalog: Path = Path("data/catalog.sqlite")
    data: Path = Path("data/lake/")
    models: Path = Path("data/models/")
    ui_db: Path = Path("data/ui.sqlite")
    backups: Path = Path("data/backups/")
    ref: Path = Path("data/ref/")
    control: Path = Path("data/control/")
    last_commit: Path = Path("data/last_commit.json")
    status: Path = Path("data/status.json")
    archive: Path = Path("data/archive/")
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
            status=root / "status.json",
            archive=root / "archive",
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
    probe_urls: list[str] = [
        "https://www.gstatic.com/generate_204",
        "https://api.anthropic.com/",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ]
    probe_timeout_seconds: float = 8.0
    probe_seconds: float = 10.0
    probe_cache_seconds: float = 5.0
    sleep_gap_seconds: float = 60.0
    resume_stagger_seconds: float = 30.0
    min_gap_seconds: float = 60.0
    catch_up_min_gap_seconds: float = 300.0
    catch_up_concurrency: int = 8
    download_deadline_seconds: float = 60.0
    allow_loopback: bool = False

    @field_validator("user_agent")
    @classmethod
    def agent_has_no_address(cls, value: str) -> str:
        if "@" in value:
            raise ValueError("the user agent must carry no email address")
        return value


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
    storyline_join: float = 0.92
    storyline_merge: float = 0.95
    storyline_promote_items: int = 3
    storyline_promote_sources: int = 2
    dedupe_cosine: float = 0.95
    dedupe_simhash_distance: int = 3
    dedupe_window_hours: int = 36
    entity_fuzzy_score: int = 92
    fetch_domain_interval_seconds: float = 3.0
    fetch_timeout_seconds: float = 10.0
    fetch_deadline_seconds: float = 30.0
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
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    lan_concurrency: int = 256
    fragment_throttle_seconds: int = 10
    slow_query_ms: int = 500
    slow_throttle_seconds: int = 20
    snapshot_poll_seconds: float = 3.0
    query_cache_size: int = 512
    default_window: str = "1h"
    timezone: str = "America/New_York"


class Disk(BaseModel):
    warn_gb: float = 10.0
    degrade_gb: float = 5.0
    autoprune_gb: float = 3.0


class Archive(BaseModel):
    enabled: bool = True
    compression_level: int = 9
    window_log: int = 26
    flush_seconds: int = 30
    min_free_gb: float = 15.0
    upload_minutes: int = 60
    local_keep_hours: int = 24
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_prefix: str = "electionnight"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    anthropic_api_key: SecretStr | None = None
    fec_api_key: SecretStr | None = None
    scratch_dir: Path | None = None
    paths: Paths = Field(default_factory=Paths)
    poll: Poll = Field(default_factory=Poll)
    commit: Commit = Field(default_factory=Commit)
    lake: Lake = Field(default_factory=Lake)
    nlp: Nlp = Field(default_factory=Nlp)
    llm: Llm = Field(default_factory=Llm)
    web: Web = Field(default_factory=Web)
    disk: Disk = Field(default_factory=Disk)
    archive: Archive = Field(default_factory=Archive)

    @field_validator("scratch_dir", mode="before")
    @classmethod
    def _blank_means_no_scratch_dir(cls, value: Any) -> Any:
        return value or None

    @model_validator(mode="after")
    def _place_paths(self) -> Self:
        """Move every path under `scratch_dir`, and refuse a partial move."""
        if self.scratch_dir is not None:
            self.paths = Paths.under(
                self.scratch_dir, config=self.scratch_dir / "config"
            )
            return self
        defaults = Paths()
        moved = [
            f"PATHS__{name.upper()}"
            for name in LAKE_PATHS
            if getattr(self.paths, name) != getattr(defaults, name)
        ]
        if moved and len(moved) < len(LAKE_PATHS):
            raise ValueError(
                f"{', '.join(moved)} would move part of the lake and leave "
                "the rest live. Set SCRATCH_DIR to place every path under "
                "one directory."
            )
        return self


def load_settings(env_file: str | None = None) -> Settings:
    """Load the dotenv file into the process, then build settings.

    Libraries that read the environment directly, such as Hugging Face
    downloads, see the same values because the file is loaded first.
    """
    path = env_file or os.environ.get("MONITOR_ENV_FILE", ".env")
    load_dotenv(path, override=False)
    settings = Settings()
    live_config = Paths().config
    if (
        settings.scratch_dir is not None
        and live_config.exists()
        and not settings.paths.config.exists()
    ):
        shutil.copytree(live_config, settings.paths.config)
    models = str(settings.paths.models.resolve())
    os.environ.setdefault("HF_HOME", models)
    os.environ.setdefault("FASTEMBED_CACHE_PATH", models)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
