import os
import shutil
import sqlite3
from pathlib import Path

import pytest

# Tests must never read real keys or settings from the repository's `.env`.
os.environ["MONITOR_ENV_FILE"] = os.devnull
# `TestClient` sends this host name.
os.environ["WEB__ALLOWED_HOSTS"] = (
    '["127.0.0.1", "localhost", "::1", "testserver"]'
)

from monitor.lake import ensure_dirs, init_lake  # noqa: E402
from monitor.settings import Paths, Settings  # noqa: E402


@pytest.fixture(scope="session")
def lake_template(tmp_path_factory) -> Path:
    """One initialized catalog. Creating a lake takes most of a second."""
    root = tmp_path_factory.mktemp("lake-template")
    init_lake(Settings(paths=Paths.under(root)))
    with sqlite3.connect(root / "catalog.sqlite") as catalog:
        catalog.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root


def clone_lake(template: Path, root: Path) -> Settings:
    """A fresh empty lake under `root`, copied from the template."""
    settings = Settings(paths=Paths.under(root, config=Path("config")))
    ensure_dirs(settings)
    shutil.copyfile(template / "catalog.sqlite", settings.paths.catalog)
    data_path = f"{settings.paths.data.resolve()}/"
    with sqlite3.connect(settings.paths.catalog) as catalog:
        catalog.execute(
            "UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'",
            (data_path,),
        )
    return settings


@pytest.fixture
def settings(lake_template, tmp_path) -> Settings:
    return clone_lake(lake_template, tmp_path)
