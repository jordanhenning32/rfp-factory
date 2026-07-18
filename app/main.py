"""Entrypoint: FastAPI + NiceGUI under one process.

NiceGUI ships its own FastAPI instance (`nicegui.app`) — we attach JSON API
routes to it and let NiceGUI handle page rendering.
"""

from __future__ import annotations

import logging

# Alias NiceGUI's `app` to avoid colliding with our own package named `app`.
from nicegui import app as nicegui_app
from nicegui import ui

from app.config import ensure_data_dirs, get_settings
from app.core.company_profile import get_profile_version
from app.services.proposals import recover_stale_busy_proposals
from app.ui._theme import ASSETS_DIR, FAVICON_FILE

# Mount the assets/ directory as a static-file route at /assets so
# templates can reference brand images via stable URLs (`/assets/brand/
# qd-logo-header.png`, etc.). Must run before any page handler tries
# to render an `ui.image()` pointing at /assets/...
nicegui_app.add_static_files("/assets", str(ASSETS_DIR))

# Importing pages registers all @ui.page() routes. Import for side effects.
import app.ui.pages  # noqa: F401, E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rfp.app")


@nicegui_app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "company_profile_version": get_profile_version(),
    }


def main() -> None:
    settings = get_settings()
    ensure_data_dirs()
    log.info("Starting RFP Factory on %s:%d (%s)", settings.app_host, settings.app_port, settings.app_env)

    # Recover orphan busy statuses left over from a prior process that
    # was killed mid-pipeline. Safe to run before NiceGUI accepts
    # requests — no background threads exist yet, so no in-flight work
    # can be disturbed by the status flip.
    try:
        recovery = recover_stale_busy_proposals()
        if recovery["reverted"] or recovery["intaking_stuck"]:
            for pid, old, new in recovery["reverted"]:
                log.info(
                    "stale-status recovery: proposal %d reverted %s -> %s",
                    pid,
                    old,
                    new,
                )
            for pid in recovery["intaking_stuck"]:
                log.info(
                    "stale-status recovery: proposal %d stuck in 'intaking' — manual Retry needed",
                    pid,
                )
    except Exception:
        log.exception(
            "stale-status recovery pass failed — continuing startup. "
            "Stuck proposals will need manual status fix-up."
        )

    ui.run(
        host=settings.app_host,
        port=settings.app_port,
        title="RFP Factory · Quadratic Digital",
        # Real Quadratic Digital favicon (cyan parabola + Q) instead of
        # the 📄 emoji. Path is absolute since NiceGUI's favicon hook
        # reads the file directly at startup, not via the static route.
        favicon=str(FAVICON_FILE),
        storage_secret=settings.app_storage_secret,
        reload=False,
        show=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
