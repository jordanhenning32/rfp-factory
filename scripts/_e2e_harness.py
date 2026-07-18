"""Shared setup for deterministic E2E scripts.

The scripts import app.db.session at module import time, so DATABASE_URL must be
selected and migrated before any app module import happens.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = (PROJECT_ROOT / "data" / "sqlite.db").resolve()


def _sqlite_path_from_url(database_url: str) -> Path | None:
    if not database_url.startswith("sqlite:///"):
        return None
    raw_path = database_url.removeprefix("sqlite:///")
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def reject_default_database(database_url: str) -> None:
    sqlite_path = _sqlite_path_from_url(database_url)
    if sqlite_path == DEFAULT_DB_PATH:
        raise RuntimeError(
            "Refusing to run deterministic E2E script against the default database: "
            f"{DEFAULT_DB_PATH}"
        )


def configure_e2e_database(script_name: str) -> Path | None:
    existing_database_url = os.environ.get("DATABASE_URL")
    if existing_database_url:
        reject_default_database(existing_database_url)

    database_url = os.environ.get("E2E_DATABASE_URL")
    db_path: Path | None = None

    if database_url:
        reject_default_database(database_url)
        os.environ["DATABASE_URL"] = database_url
        db_path = _sqlite_path_from_url(database_url)
    else:
        db_path = Path(tempfile.mkdtemp(prefix=f"{Path(script_name).stem}-")) / "e2e.sqlite"
        database_url = f"sqlite:///{db_path}"
        reject_default_database(database_url)
        os.environ["DATABASE_URL"] = database_url

    print(f"E2E database: {db_path if db_path else database_url}")
    print("Running Alembic migrations for E2E database...")
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
    )
    return db_path
