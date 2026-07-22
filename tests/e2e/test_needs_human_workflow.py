"""Real-browser coverage for the Draft tab's NEEDS_HUMAN lifecycle."""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import expect

from tests.e2e.conftest import BrowserSession, E2EServer

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TITLE = "Synthetic NEEDS_HUMAN Lifecycle RFP"
PROVIDE_MARKER = "insert final transition-plan artifact name"
SIGN_MARKER = "authorized company representative signature"
REMOVE_MARKER = "confirm whether to retain the weekly legacy-reporting statement"
REPLACEMENT = "Transition Readiness Evidence Matrix"
COMMITMENT = "Attach the final Transition Readiness Evidence Matrix"
SIGNER = "Jordan E2E Signatory"
SIGN_DATE = "July 21, 2032"
SIGNATURE = f"/s/ {SIGNER} — {SIGN_DATE}"


@pytest.fixture()
def needs_human_seed(e2e_server: E2EServer):
    ledger_path = e2e_server.workspace.artifacts / "llm_calls.jsonl"
    ledger_start = (
        len(ledger_path.read_text(encoding="utf-8").splitlines())
        if ledger_path.exists()
        else 0
    )
    script = (
        PROJECT_ROOT / "tests" / "e2e" / "support" / "seed_needs_human_data.py"
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
    seed_log = e2e_server.workspace.artifacts / "needs_human_seed.log"
    seed_log.write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if result.returncode != 0:
        pytest.fail(
            f"synthetic NEEDS_HUMAN seed failed (exit {result.returncode}); "
            f"see {seed_log}",
            pytrace=False,
        )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(
            f"NEEDS_HUMAN seed returned invalid JSON: {exc}; see {seed_log}",
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
    cleanup_log = e2e_server.workspace.artifacts / "needs_human_cleanup.log"
    cleanup_log.write_text(
        (cleanup.stdout or "") + (cleanup.stderr or ""), encoding="utf-8"
    )
    if cleanup.returncode != 0:
        pytest.fail(
            f"synthetic NEEDS_HUMAN cleanup failed (exit {cleanup.returncode}); "
            f"see {cleanup_log}",
            pytrace=False,
        )


@pytest.fixture()
def needs_human_browser(
    needs_human_seed: dict[str, int],
    browser_session: BrowserSession,
) -> tuple[BrowserSession, dict[str, int]]:
    return browser_session, needs_human_seed


def _fetchone(
    database_path: Path,
    sql: str,
    params: tuple[Any, ...] = (),
) -> sqlite3.Row | None:
    with sqlite3.connect(database_path, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        return db.execute(sql, params).fetchone()


def _eventually(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    description: str,
    timeout: float = 20.0,
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
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {description}; "
        f"last={last_value!r}, error={last_error!r}"
    )


def _proposal_tab(page, label: str):
    label_node = page.locator(".q-tab__label").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    )
    return page.get_by_role("tab").filter(has=label_node).first


def _open_tab(page, label: str, visible_text: str) -> None:
    tab = _proposal_tab(page, label)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    page.get_by_text(visible_text, exact=False).first.wait_for(
        state="visible", timeout=10_000
    )
    expect(tab).to_have_attribute("aria-selected", "true", timeout=10_000)


def _placeholder_card(page, marker: str):
    marker_label = page.get_by_text(marker, exact=True).filter(visible=True).first
    marker_label.wait_for(state="visible", timeout=10_000)
    return marker_label.locator(
        "xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), "
        "' q-card ')][1]"
    )


def _notification(page, text: str):
    notice = page.locator(".q-notification").filter(has_text=text).last
    notice.wait_for(state="visible", timeout=10_000)
    return notice


def _section_row(database_path: Path, section_id: int) -> sqlite3.Row | None:
    return _fetchone(
        database_path,
        "SELECT draft_text_markdown, needs_human_placeholders_json, "
        "current_revision_number FROM proposal_sections WHERE id = ?",
        (section_id,),
    )


def _new_ledger_calls(path: Path, start: int) -> list[tuple[str, str, str | None]]:
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()[start:]
        if line.strip()
    ]
    return [
        (entry["method"], entry["agent_name"], entry.get("tool_name"))
        for entry in entries
    ]


def test_needs_human_actions_audit_commitment_and_readiness(
    needs_human_browser: tuple[BrowserSession, dict[str, int]],
) -> None:
    session, seed = needs_human_browser
    page = session.page
    server = session.server
    database_path = server.workspace.database_path
    proposal_id = int(seed["proposal_id"])
    section_id = int(seed["section_id"])

    response = page.goto(
        f"{server.base_url}/proposals/{proposal_id}",
        wait_until="load",
    )
    assert response is not None and response.status == 200
    page.get_by_text(TITLE, exact=True).wait_for(state="visible", timeout=15_000)
    page.get_by_text("3 action items", exact=False).first.wait_for(
        state="visible", timeout=10_000
    )
    page.get_by_text("rev 1", exact=True).wait_for(state="visible")

    # The authoritative approval action exposes the initial readiness blocker.
    page.get_by_role(
        "button", name="Approve for submission", exact=True
    ).click()
    initial_block = _notification(page, "Approval blocked:")
    expect(initial_block).to_contain_text(
        "All NEEDS_HUMAN placeholders resolved: 3 placeholder(s) still pending"
    )
    assert _fetchone(
        database_path,
        "SELECT status FROM proposals WHERE id = ?",
        (proposal_id,),
    )["status"] == "draft_ready"

    # Provide value. Waiting for the seeded initial suggestion proves the
    # advisor call stayed inside the deterministic fake-provider boundary;
    # conversational chat is deliberately out of scope for this lifecycle.
    provide_card = _placeholder_card(page, PROVIDE_MARKER)
    provide_card.get_by_role(
        "button", name="Provide value", exact=True
    ).click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text("Provide value", exact=True).wait_for(state="visible")
    dialog.get_by_text(REPLACEMENT, exact=True).wait_for(
        state="visible", timeout=10_000
    )
    dialog.get_by_role("button", name="Use this", exact=True).click()
    replacement_input = dialog.get_by_label("Replacement text", exact=True)
    expect(replacement_input).to_have_value(REPLACEMENT)
    commitment_opt_in = dialog.get_by_role(
        "checkbox",
        name="Add this commitment to the Submission Checklist",
        exact=True,
    )
    commitment_opt_in.click()
    expect(commitment_opt_in).to_have_attribute("aria-checked", "true")
    dialog.get_by_label(
        "Commitment description (editable)", exact=True
    ).fill(COMMITMENT)
    dialog.get_by_role("button", name="Apply", exact=True).click()
    _notification(page, "Value applied to the draft.")

    provided = _eventually(
        lambda: _section_row(database_path, section_id),
        lambda row: bool(
            row
            and row["current_revision_number"] == 2
            and REPLACEMENT in row["draft_text_markdown"]
            and f"[NEEDS_HUMAN: {PROVIDE_MARKER}]"
            not in row["draft_text_markdown"]
        ),
        description="provided value and revision 2",
    )
    assert provided is not None

    # Sign with a deterministic typed name/date and verify the exact inline
    # electronic-signature representation.
    sign_card = _placeholder_card(page, SIGN_MARKER)
    sign_card.get_by_role("button", name="Sign", exact=True).click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text(
        "Apply Electronic Signature", exact=True
    ).wait_for(state="visible")
    dialog.get_by_label("Signed by", exact=True).fill(SIGNER)
    dialog.get_by_label("Date", exact=True).fill(SIGN_DATE)
    dialog.get_by_text(SIGNATURE, exact=True).wait_for(state="visible")
    dialog.get_by_role(
        "button", name="Apply Signature", exact=True
    ).click()
    _notification(page, "Signature applied.")
    signed = _eventually(
        lambda: _section_row(database_path, section_id),
        lambda row: bool(
            row
            and row["current_revision_number"] == 3
            and SIGNATURE in row["draft_text_markdown"]
            and f"[NEEDS_HUMAN: {SIGN_MARKER}]" not in row["draft_text_markdown"]
        ),
        description="signature and revision 3",
    )
    assert signed is not None

    # Reject/remove the obsolete optional marker. The product's supported
    # recovery path is section regeneration, which the confirmation explains;
    # there is no direct reopen/undo control for resolved placeholders.
    remove_card = _placeholder_card(page, REMOVE_MARKER)
    remove_card.get_by_role("button", name="Remove", exact=True).click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text("Remove placeholder?", exact=True).wait_for(
        state="visible"
    )
    expect(dialog).to_contain_text("To restore it, regenerate this section.")
    dialog.get_by_role(
        "button", name="Remove from draft", exact=True
    ).click()
    _notification(page, "Placeholder removed.")

    resolved_row = _eventually(
        lambda: _section_row(database_path, section_id),
        lambda row: bool(
            row
            and row["current_revision_number"] == 4
            and "[NEEDS_HUMAN:" not in row["draft_text_markdown"]
        ),
        description="all placeholder edits and revision 4",
    )
    assert resolved_row is not None
    markdown = resolved_row["draft_text_markdown"]
    assert REPLACEMENT in markdown
    assert SIGNATURE in markdown
    assert "Optional legacy reporting note:" in markdown
    assert REMOVE_MARKER not in markdown

    placeholders = json.loads(resolved_row["needs_human_placeholders_json"])
    by_marker = {item["marker_text"]: item for item in placeholders}
    assert by_marker[PROVIDE_MARKER] == {
        "marker_text": PROVIDE_MARKER,
        "description": (
            "Name the final transition-plan artifact promised to evaluators."
        ),
        "category": "schedule_commitment",
        "resolved": True,
        "resolution_kind": "edit",
        "resolution_value": REPLACEMENT,
    }
    assert by_marker[SIGN_MARKER]["resolved"] is True
    assert by_marker[SIGN_MARKER]["resolution_kind"] == "signature"
    assert by_marker[SIGN_MARKER]["resolution_value"] == SIGNATURE
    assert by_marker[REMOVE_MARKER]["resolved"] is True
    assert by_marker[REMOVE_MARKER]["resolution_kind"] == "reject"
    assert by_marker[REMOVE_MARKER]["resolution_value"] == ""
    page.get_by_text("rev 4", exact=True).wait_for(state="visible")
    page.get_by_text("All 3 placeholders resolved", exact=False).wait_for(
        state="visible"
    )
    assert page.get_by_role("button", name=re.compile("^(Reopen|Undo)$")).count() == 0

    # The Provide-value opt-in creates one linked, pending submission
    # commitment, while the advisor provider call leaves its standard AgentRun
    # audit row and deterministic call-ledger entry.
    commitment = _fetchone(
        database_path,
        "SELECT id, description, source, source_section_id, obtained "
        "FROM submission_commitments WHERE proposal_id = ?",
        (proposal_id,),
    )
    assert commitment is not None
    assert dict(commitment) == {
        "id": commitment["id"],
        "description": COMMITMENT,
        "source": "needs_human_apply",
        "source_section_id": section_id,
        "obtained": 0,
    }
    advisor_run = _fetchone(
        database_path,
        "SELECT status, error_text FROM agent_runs WHERE proposal_id = ? "
        "AND agent_name = 'needs_human_advisor' ORDER BY id DESC LIMIT 1",
        (proposal_id,),
    )
    assert advisor_run is not None
    assert advisor_run["status"] == "completed"
    assert advisor_run["error_text"] is None

    # Resolving the draft clears the placeholder check, but it also changes
    # the reviewed content from revision 1 to revision 4. Readiness must not
    # reuse the seeded revision-1 review marker.
    _open_tab(page, "Submission Checklist", "System-verified readiness")
    page.get_by_text("8/9 verified", exact=True).wait_for(state="visible")
    page.get_by_role(
        "button", name="Approve for submission", exact=True
    ).click()
    stale_review_block = _notification(page, "Approval blocked:")
    expect(stale_review_block).to_contain_text(
        "Proposal reviewer run: 0/1 current section revision(s) fully reviewed"
    )
    expect(stale_review_block).to_contain_text(
        f"Submission commitment: {COMMITMENT}"
    )

    # Exercise the real reviewer orchestration against the current revision.
    # The deterministic A/B fixtures return clean results; production writes
    # the composite coverage marker only after both calls and persistence.
    _open_tab(page, "Reviewer Findings", "No reviewer findings yet.")
    page.get_by_role(
        "button", name="Run Auto Review-Revise Loop", exact=True
    ).first.click()
    from app.services.review_coverage import review_coverage_prompt_version

    current_review_key = review_coverage_prompt_version(section_id, 4)
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT status FROM agent_runs WHERE proposal_id = ? "
            "AND agent_name = '_review_coverage' AND prompt_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (proposal_id, current_review_key),
        ),
        lambda row: row is not None and row["status"] == "completed",
        description="clean composite review coverage for revision 4",
    )
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None and row["status"] == "draft_ready",
        description="proposal restored after current-revision review",
    )

    page.goto(
        f"{server.base_url}/proposals/{proposal_id}", wait_until="load",
    )
    page.get_by_text(TITLE, exact=True).wait_for(state="visible")
    _open_tab(page, "Submission Checklist", "System-verified readiness")
    page.get_by_text("9/9 verified", exact=True).wait_for(state="visible")
    readiness_row = page.get_by_text(
        "All NEEDS_HUMAN placeholders resolved", exact=True
    ).locator("xpath=ancestor::*[contains(@class, 'row')][1]")
    expect(readiness_row).to_contain_text("All resolved")
    commitment_card = page.locator(".q-card").filter(has_text=COMMITMENT).last
    commitment_card.wait_for(state="visible")

    page.get_by_role(
        "button", name="Approve for submission", exact=True
    ).click()
    commitment_block = _notification(page, "Approval blocked:")
    expect(commitment_block).to_contain_text(
        f"Submission commitment: {COMMITMENT}"
    )
    assert _fetchone(
        database_path,
        "SELECT status FROM proposals WHERE id = ?",
        (proposal_id,),
    )["status"] == "draft_ready"

    # Quasar replaces this checkbox immediately when the refreshable tab
    # redraws, so a user-style click is the stable interaction; persistence
    # below is the authoritative state assertion.
    commitment_card.get_by_role("checkbox").click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT obtained FROM submission_commitments WHERE id = ?",
            (commitment["id"],),
        ),
        lambda row: row is not None and row["obtained"] == 1,
        description="obtained submission commitment",
    )

    # Wait for the product's refresh messages to replace both the checklist
    # row and next-step banner before interacting with their new elements.
    refreshed_commitment_card = page.locator(".q-card").filter(
        has_text=COMMITMENT
    ).last
    expect(
        refreshed_commitment_card.get_by_role("checkbox")
    ).to_have_attribute("aria-checked", "true")
    refreshed_approve = page.get_by_role(
        "button", name="Approve for submission", exact=True
    )
    refreshed_approve.wait_for(state="visible")
    refreshed_approve.click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT status FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None and row["status"] == "approved",
        description="fully ready proposal approval",
    )
    page.get_by_text(
        "Approved — submit through the agency's system", exact=True
    ).wait_for(state="visible", timeout=10_000)

    ledger_path = server.workspace.artifacts / "llm_calls.jsonl"
    ledger_calls = _new_ledger_calls(
        ledger_path, int(seed["ledger_start"])
    )
    assert ledger_calls[0] == (
        "call_tool",
        "needs_human_advisor",
        "report_suggested_replacement",
    )
    assert sorted(ledger_calls[1:]) == sorted([
        ("call_tool", "reviewer_a", "report_findings"),
        ("call_tool", "reviewer_b", "report_findings"),
    ])
