"""Start the real application with test-only LLM providers installed.

This is a subprocess entrypoint, not production application code. The parent
pytest process prepares and migrates a disposable data root before launching
it. Guardrails below reject the canonical repository data tree even if the
launcher is invoked manually with a bad environment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _validate_environment() -> Path:
    if os.environ.get("APP_ENV", "").strip().lower() != "e2e":
        raise RuntimeError("E2E server requires APP_ENV=e2e")
    if os.environ.get("RFP_E2E_FAKE_LLM", "") != "1":
        raise RuntimeError("E2E server requires RFP_E2E_FAKE_LLM=1")
    if os.environ.get("APP_HOST", "").strip() != "127.0.0.1":
        raise RuntimeError("E2E server must bind to APP_HOST=127.0.0.1")

    raw_data_dir = os.environ.get("RFP_DATA_DIR", "").strip()
    if not raw_data_dir:
        raise RuntimeError("E2E server requires an explicit RFP_DATA_DIR")
    data_dir = Path(raw_data_dir).resolve()

    canonical_data = (PROJECT_ROOT / "data").resolve()
    try:
        data_dir.relative_to(canonical_data)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            f"Refusing to run E2E against canonical/demo data: {data_dir}"
        )

    database_url = os.environ.get("DATABASE_URL", "").replace("\\", "/")
    expected_db = (data_dir / "sqlite.db").as_posix()
    if not database_url.startswith("sqlite:///") or expected_db.lower() not in database_url.lower():
        raise RuntimeError(
            "E2E DATABASE_URL must point to sqlite.db inside RFP_DATA_DIR; "
            f"got {database_url!r}"
        )
    return data_dir


def main() -> None:
    data_dir = _validate_environment()

    # Import and patch the provider module before app.main imports pages,
    # jobs, and agents (many of which import the dispatch functions by name).
    from tests.e2e.support.fake_llm import install_fixture_llm

    install_fixture_llm()

    from app.main import main as run_application

    print(f"Starting isolated E2E application with data root {data_dir}", flush=True)
    run_application()


if __name__ == "__main__":
    main()
