"""Minimum real-browser proof that the isolated application is usable."""
from __future__ import annotations

import sqlite3

import pytest

from tests.e2e.conftest import BrowserSession

pytestmark = pytest.mark.e2e


def test_health_and_empty_pipeline_root(browser_session: BrowserSession) -> None:
    page = browser_session.page
    server = browser_session.server

    health = page.request.get(f"{server.base_url}/api/health")
    assert health.ok, health.text()
    assert health.json() == {
        "ok": True,
        "company_profile_version": "e2e-1.0.0",
    }

    response = page.goto(server.base_url, wait_until="domcontentloaded")
    assert response is not None
    assert response.status == 200
    page.get_by_text("Proposals in flight", exact=True).wait_for(
        state="visible", timeout=10_000,
    )
    page.get_by_text(
        "No proposals yet. Start one from the New Proposal page.", exact=True,
    ).wait_for(state="visible", timeout=10_000)
    assert page.get_by_text("New Proposal", exact=True).count() >= 1

    with sqlite3.connect(server.workspace.database_path) as db:
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()
        proposal_count = db.execute("SELECT COUNT(*) FROM proposals").fetchone()
    assert revision and revision[0]
    assert proposal_count == (0,)
