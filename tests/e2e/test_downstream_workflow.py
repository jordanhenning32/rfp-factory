"""Real-browser proof of the complete post-outline IT-services workflow.

This suite seeds only completed upstream work.  Every transition under test is
driven through the actual NiceGUI controls and persisted by production jobs;
the only fake boundary is the deterministic, audited LLM provider fixture.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import BrowserSession, E2EServer

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TITLE = "Synthetic Downstream IT Services RFP"
NARRATIVE_BEFORE_POLISH = "The delivery plan uses a four-person team."
NARRATIVE_AFTER_POLISH = (
    "The delivery plan uses one approved program manager."
)

EXPECTED_DOWNSTREAM_CALLS = Counter(
    {
        ("call_tool", "cost_analyst", "report_cost_analysis"): 1,
        ("call_tool", "cost_writer", "draft_cost_section"): 1,
        ("call_tool", "writer_team", "report_section_draft"): 2,
        (
            "call_tool",
            "cost_reviewer:gemini-2.5-pro",
            "report_cost_review_findings",
        ): 1,
        (
            "call_tool",
            "cost_reviewer:gpt-5.5",
            "report_cost_review_findings",
        ): 1,
        (
            "call_tool",
            "reviewer_a",
            "report_findings",
        ): 4,
        ("call_tool", "reviewer_b", "report_findings"): 4,
        (
            "call_tool",
            "consistency_checker",
            "report_inconsistencies",
        ): 2,
        (
            "call_tool",
            "final_polish_detector",
            "report_polish_issues",
        ): 1,
        (
            "call_tool",
            "final_polish_applier",
            "report_polished_section",
        ): 1,
    }
)


@pytest.fixture()
def downstream_seed(e2e_server: E2EServer):
    ledger_path = e2e_server.workspace.artifacts / "llm_calls.jsonl"
    ledger_start = (
        len(ledger_path.read_text(encoding="utf-8").splitlines())
        if ledger_path.exists()
        else 0
    )
    script = (
        PROJECT_ROOT / "tests" / "e2e" / "support" / "seed_downstream_data.py"
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        env=e2e_server.workspace.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    seed_log = e2e_server.workspace.artifacts / "downstream_seed.log"
    seed_log.write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if result.returncode != 0:
        pytest.fail(
            f"synthetic downstream seed failed (exit {result.returncode}); "
            f"see {seed_log}",
            pytrace=False,
        )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(
            f"downstream seed returned invalid JSON: {exc}; see {seed_log}",
            pytrace=False,
        )
    payload["ledger_start"] = ledger_start
    yield payload

    cleanup = subprocess.run(
        [sys.executable, str(script), "--cleanup"],
        cwd=PROJECT_ROOT,
        env=e2e_server.workspace.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    cleanup_log = e2e_server.workspace.artifacts / "downstream_cleanup.log"
    cleanup_log.write_text(
        (cleanup.stdout or "") + (cleanup.stderr or ""), encoding="utf-8"
    )
    if cleanup.returncode != 0:
        pytest.fail(
            f"synthetic downstream cleanup failed (exit {cleanup.returncode}); "
            f"see {cleanup_log}",
            pytrace=False,
        )


@pytest.fixture()
def downstream_browser(
    downstream_seed: dict[str, int],
    browser_session: BrowserSession,
) -> tuple[BrowserSession, dict[str, int]]:
    return browser_session, downstream_seed


def _fetchone(
    database_path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Row | None:
    with sqlite3.connect(database_path, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchone()


def _fetchall(
    database_path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchall()


def _eventually(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    description: str,
    timeout: float = 60.0,
) -> Any:
    deadline = time.monotonic() + timeout
    last_value: Any = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            last_value = probe()
            last_error = None
            if predicate(last_value):
                return last_value
        except sqlite3.OperationalError as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f"last value={last_value!r}"
    if last_error is not None:
        detail += f", last SQLite error={last_error!r}"
    raise AssertionError(f"Timed out waiting for {description}; {detail}")


def _wait_for_status(
    database_path: Path,
    proposal_id: int,
    expected: str,
) -> sqlite3.Row:
    row = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT id, status, team_approved_at, submitted_at "
            "FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda value: value is not None and value["status"] == expected,
        description=f"proposal #{proposal_id} status={expected!r}",
    )
    assert isinstance(row, sqlite3.Row)
    return row


def _goto_proposal(page, base_url: str, proposal_id: int) -> None:
    # Background jobs can finish before the progress route's deferred static
    # modules do. Let the current document finish loading before navigating so
    # Chromium does not report intentional asset cancellations as failed local
    # requests, then require the destination's full load event as well.
    if page.url != "about:blank":
        page.wait_for_load_state("load", timeout=10_000)
    response = page.goto(
        f"{base_url}/proposals/{proposal_id}",
        wait_until="load",
    )
    assert response is not None and response.status == 200
    page.get_by_text(TITLE, exact=True).wait_for(state="visible", timeout=15_000)


def _proposal_tab(page, label: str):
    label_node = page.locator(".q-tab__label").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    )
    return page.get_by_role("tab").filter(has=label_node).first


def _open_tab(page, label: str) -> None:
    from playwright.sync_api import expect

    tab = _proposal_tab(page, label)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true", timeout=5_000)


def _set_qselect(page, dialog, label: str, value: str) -> None:
    label_node = dialog.locator(".q-field__label").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    ).first
    label_node.wait_for(state="visible", timeout=5_000)
    field = label_node.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
        "' q-field ')][1]"
    )
    field.click()
    field.locator("input").first.fill(value)
    field.locator("input").first.press("Enter")


def _add_member(
    page,
    *,
    role: str,
    person: str,
    experience: str,
    bio: str,
) -> None:
    page.get_by_role("button", name="Add Team Member", exact=True).click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text("Add Team Member", exact=True).wait_for(
        state="visible", timeout=5_000
    )
    _set_qselect(page, dialog, "Role name", role)
    _set_qselect(page, dialog, "Assigned person", person)
    _set_qselect(page, dialog, "Labor category (GSA OLM)", "Project Manager I")
    dialog.get_by_label("Salary", exact=True).fill("150K")
    dialog.get_by_label("Experience (yrs)", exact=True).fill(experience)
    dialog.get_by_label("Active phases (comma-separated)", exact=True).fill(
        "Delivery"
    )
    dialog.get_by_label("Bio summary (1-2 sentences)", exact=True).fill(bio)
    # QSelect's add-unique value travels over the NiceGUI websocket. Give
    # those queued value-change events one short turn before Save reads the
    # server-side element values; otherwise a fast headless browser can race
    # the handler even though Chromium already paints the selected text.
    page.wait_for_timeout(500)
    dialog.get_by_role("button", name="Save", exact=True).click()
    dialog.wait_for(state="hidden", timeout=10_000)
    page.get_by_text(role, exact=True).wait_for(state="visible", timeout=5_000)


def _member_card(page, role: str):
    return page.locator(".q-card").filter(
        has=page.get_by_text(role, exact=True)
    ).last


def _new_ledger_calls(path: Path, start: int) -> Counter:
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()[start:]
        if line.strip()
    ]
    return Counter(
        (
            entry["method"],
            entry["agent_name"],
            entry.get("tool_name"),
        )
        for entry in entries
    )


def test_downstream_workflow_through_archive(
    downstream_browser: tuple[BrowserSession, dict[str, int]],
) -> None:
    from playwright.sync_api import expect

    session, seed = downstream_browser
    page = session.page
    server = session.server
    base_url = server.base_url
    database_path = server.workspace.database_path
    proposal_id = int(seed["proposal_id"])

    _goto_proposal(page, base_url, proposal_id)
    _wait_for_status(database_path, proposal_id, "awaiting_team_approval")
    expect(
        page.get_by_text("Action needed: build and approve the team", exact=True)
    ).to_be_visible()
    _open_tab(page, "Team")

    # Team CRUD through the actual dialog/card controls: create, read, update,
    # and delete a transient row before approving the retained roster.
    _add_member(
        page,
        role="Program Manager",
        person="Alex E2E Morgan",
        experience="8",
        bio="Synthetic program manager for deterministic browser coverage.",
    )
    member = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT id, assigned_person, labor_category, wage_band, "
            "time_allocation_pct, experience_years, bio_summary "
            "FROM proposal_team_members WHERE proposal_id = ? "
            "AND role_name = 'Program Manager'",
            (proposal_id,),
        ),
        lambda row: row is not None,
        description="created Program Manager team row",
    )
    assert member["assigned_person"] == "Alex E2E Morgan"
    assert member["labor_category"] == "Project Manager I"
    assert member["wage_band"] == "150K"
    assert member["time_allocation_pct"] == 50

    program_card = _member_card(page, "Program Manager")
    expect(program_card.get_by_text("Alex E2E Morgan", exact=False)).to_be_visible()
    program_card.locator("button").first.click()
    edit_dialog = page.get_by_role("dialog").last
    edit_dialog.get_by_text("Edit Team Member", exact=True).wait_for()
    edit_dialog.get_by_label("Experience (yrs)", exact=True).fill("9")
    edit_dialog.get_by_label(
        "Bio summary (1-2 sentences)", exact=True
    ).fill("Updated synthetic program-management bio for CRUD coverage.")
    page.wait_for_timeout(250)
    edit_dialog.get_by_role("button", name="Save", exact=True).click()
    edit_dialog.wait_for(state="hidden", timeout=10_000)
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT experience_years, bio_summary FROM proposal_team_members "
            "WHERE id = ?",
            (member["id"],),
        ),
        lambda row: row is not None and row["experience_years"] == 9,
        description="updated Program Manager team row",
    )

    _add_member(
        page,
        role="Transient QA Role",
        person="Taylor Temporary E2E",
        experience="3",
        bio="Synthetic row created only to exercise the delete behavior.",
    )
    transient = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT id FROM proposal_team_members WHERE proposal_id = ? "
            "AND role_name = 'Transient QA Role'",
            (proposal_id,),
        ),
        lambda row: row is not None,
        description="created transient team row",
    )
    _member_card(page, "Transient QA Role").locator("button").last.click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT COUNT(*) AS n FROM proposal_team_members "
            "WHERE proposal_id = ? AND role_name = 'Transient QA Role'",
            (proposal_id,),
        ),
        lambda row: row is not None and row["n"] == 0,
        description=f"deleted transient team row #{transient['id']}",
    )
    page.get_by_text("Transient QA Role", exact=True).wait_for(
        state="hidden", timeout=10_000
    )
    page.get_by_text("Pending approval (1 member)", exact=True).wait_for(
        state="visible", timeout=10_000
    )

    assert _wait_for_status(
        database_path, proposal_id, "awaiting_team_approval"
    )["team_approved_at"] is None
    approve_team_button = page.get_by_role(
        "button", name="Approve Team", exact=True
    )
    expect(approve_team_button).to_be_enabled()
    # The delete handler refreshes the Team subtree. Wait for that refresh to
    # settle so this click cannot land on the just-detached button instance.
    page.wait_for_timeout(500)
    approve_team_button.click()
    try:
        _eventually(
            lambda: _fetchone(
                database_path,
                "SELECT status FROM proposals WHERE id = ?",
                (proposal_id,),
            ),
            lambda row: row is not None and row["status"] == "awaiting_cost_build",
            description="team approval click",
            timeout=5.0,
        )
    except AssertionError:
        # Idempotent retry for a websocket event that raced a final refresh.
        page.get_by_role("button", name="Approve Team", exact=True).click()
    approved_team = _wait_for_status(
        database_path, proposal_id, "awaiting_cost_build"
    )
    assert approved_team["team_approved_at"] is not None

    # The MarketScan is the one bounded prerequisite seam. Cost Analyst and
    # all pricing persistence below are the actual production job/math layer.
    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost")
    page.get_by_role("button", name="Run Cost Analyst", exact=True).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT COUNT(*) AS packages, "
            "COUNT(DISTINCT scenario) AS scenarios "
            "FROM pricing_packages WHERE proposal_id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None
        and row["packages"] == 3
        and row["scenarios"] == 3,
        description="three deterministic pricing scenarios",
    )
    _wait_for_status(database_path, proposal_id, "awaiting_draft")
    pricing_lines = _fetchall(
        database_path,
        "SELECT pp.scenario, pl.labor_category, pl.wage_band, pl.hours "
        "FROM pricing_package_lines AS pl "
        "JOIN pricing_packages AS pp ON pp.id = pl.pricing_package_id "
        "WHERE pp.proposal_id = ? ORDER BY pp.scenario",
        (proposal_id,),
    )
    assert len(pricing_lines) == 3
    assert {row["labor_category"] for row in pricing_lines} == {
        "Project Manager I"
    }
    assert {row["wage_band"] for row in pricing_lines} == {"150k"}
    assert {float(row["hours"]) for row in pricing_lines} == {975.0}

    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost")
    page.get_by_role(
        "button", name="Run Cost Volume Writer", exact=True
    ).last.click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    cost_section = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT draft_text_markdown, current_revision_number "
            "FROM proposal_sections WHERE proposal_id = ? "
            "AND section_id = 'SEC-103'",
            (proposal_id,),
        ),
        lambda row: row is not None
        and bool(row["draft_text_markdown"])
        and row["current_revision_number"] == 1,
        description="persisted Cost Writer draft",
    )
    assert "one approved Project Manager I" in cost_section["draft_text_markdown"]

    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role("button", name="Begin Drafting", exact=True).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT COUNT(*) AS n FROM proposal_sections "
            "WHERE proposal_id = ? AND requires_cost_analysis = 0 "
            "AND draft_text_markdown IS NOT NULL "
            "AND current_revision_number = 1",
            (proposal_id,),
        ),
        lambda row: row is not None and row["n"] == 2,
        description="two persisted initial Writer Team drafts",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")
    sec_101 = _fetchone(
        database_path,
        "SELECT draft_text_markdown FROM proposal_sections "
        "WHERE proposal_id = ? AND section_id = 'SEC-101'",
        (proposal_id,),
    )
    assert sec_101 is not None
    assert NARRATIVE_BEFORE_POLISH in sec_101["draft_text_markdown"]

    # Cost Reviewer must make both independent model calls and the consensus
    # call.  A clean result persists no findings, so AgentRun is the marker.
    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost Review")
    page.get_by_role("button", name="Run Cost Reviewer", exact=True).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT COUNT(*) AS n FROM agent_runs WHERE proposal_id = ? "
            "AND agent_name LIKE 'cost_reviewer:%' AND status = 'completed'",
            (proposal_id,),
        ),
        lambda row: row is not None and row["n"] == 2,
        description="two completed Cost Reviewer provider passes",
    )
    assert _fetchone(
        database_path,
        "SELECT COUNT(*) AS n FROM cost_review_findings AS f "
        "JOIN pricing_packages AS pp ON pp.id = f.pricing_package_id "
        "WHERE pp.proposal_id = ?",
        (proposal_id,),
    )["n"] == 0

    # The main auto-loop calls Reviewer A and B for both narrative sections,
    # then Reviewer C once across the resulting corpus.  All are clean.
    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role(
        "button", name="Run Auto Review-Revise Loop", exact=True
    ).click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT "
            "SUM(CASE WHEN agent_name = 'reviewer_a' "
            "AND status = 'completed' THEN 1 ELSE 0 END) AS a_runs, "
            "SUM(CASE WHEN agent_name = 'reviewer_b' "
            "AND status = 'completed' THEN 1 ELSE 0 END) AS b_runs, "
            "SUM(CASE WHEN agent_name = 'consistency_checker' "
            "AND status = 'completed' THEN 1 ELSE 0 END) AS c_runs "
            "FROM agent_runs WHERE proposal_id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None
        and row["a_runs"] == 2
        and row["b_runs"] == 2
        and row["c_runs"] == 1,
        description="clean Reviewer A/B and consistency passes",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")
    assert _fetchone(
        database_path,
        "SELECT COUNT(*) AS n FROM reviewer_findings AS rf "
        "JOIN proposal_sections AS ps ON ps.id = rf.proposal_section_id "
        "WHERE ps.proposal_id = ?",
        (proposal_id,),
    )["n"] == 0

    # Final Polish exercises both legs: detector issue and applier update.
    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Final Polish")
    page.get_by_role("button", name="Run Final Polish", exact=True).click()
    polish_edit = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT section_id_label, issue_type, severity, edit_summary "
            "FROM polish_edits WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ),
        lambda row: row is not None,
        description="persisted Final Polish edit",
    )
    assert polish_edit["section_id_label"] == "SEC-101"
    assert polish_edit["issue_type"] == "numerical_drift"
    polished_section = _fetchone(
        database_path,
        "SELECT id, draft_text_markdown, current_revision_number "
        "FROM proposal_sections WHERE proposal_id = ? AND section_id = 'SEC-101'",
        (proposal_id,),
    )
    assert polished_section is not None
    assert polished_section["current_revision_number"] == 2
    assert NARRATIVE_AFTER_POLISH in polished_section["draft_text_markdown"]
    assert NARRATIVE_BEFORE_POLISH not in polished_section["draft_text_markdown"]

    # Final Polish changed SEC-101 after its initial A/B pass. Re-review the
    # current revision; historical clean provider calls must not certify the
    # newly persisted content.
    from app.services.review_coverage import review_coverage_prompt_version

    polished_review_key = review_coverage_prompt_version(
        polished_section["id"], polished_section["current_revision_number"],
    )
    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role(
        "button", name="Run Auto Review-Revise Loop", exact=True
    ).click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT status FROM agent_runs WHERE proposal_id = ? "
            "AND agent_name = '_review_coverage' AND prompt_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (proposal_id, polished_review_key),
        ),
        lambda row: row is not None and row["status"] == "completed",
        description="current-revision review after Final Polish",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")

    # Approval is a fail-closed readiness gate.  Success proves every system
    # check above (team, cost, drafts, reviewers, findings/placeholders/gaps).
    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role(
        "button", name="Approve for submission", exact=True
    ).click()
    _wait_for_status(database_path, proposal_id, "approved")
    page.get_by_text(
        "Approved — submit through the agency's system", exact=True
    ).wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="Mark as submitted", exact=True).click()
    submit_dialog = page.get_by_role("dialog").last
    submit_dialog.get_by_text("Confirm submission", exact=True).wait_for()
    submit_dialog.get_by_role(
        "button", name="Confirm submitted", exact=True
    ).click()
    submitted = _wait_for_status(database_path, proposal_id, "submitted")
    assert submitted["submitted_at"] is not None

    page.get_by_role("button", name="Archive proposal", exact=True).click()
    archive_dialog = page.get_by_role("dialog").last
    archive_dialog.get_by_text("Archive proposal?", exact=True).wait_for()
    archive_dialog.get_by_role("button", name="Archive", exact=True).click()
    _wait_for_status(database_path, proposal_id, "archived")
    page.get_by_text("Archived (read-only)", exact=True).wait_for(
        state="visible", timeout=10_000
    )
    _open_tab(page, "Team")
    expect(
        page.get_by_role("button", name="Add Team Member", exact=True)
    ).to_be_disabled()

    ledger_path = server.workspace.artifacts / "llm_calls.jsonl"
    assert ledger_path.is_file()
    assert _new_ledger_calls(
        ledger_path, int(seed["ledger_start"])
    ) == EXPECTED_DOWNSTREAM_CALLS
