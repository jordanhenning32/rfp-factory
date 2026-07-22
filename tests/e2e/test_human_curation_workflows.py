"""Browser coverage for deterministic, user-curated proposal state."""
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
IT_TITLE = "Synthetic IT Modernization RFP"
DECISION_TOPIC = "Synthetic browser curation policy"
INITIAL_GAP_NOTE = (
    "Use the validated transition playbook and map each gate to buyer "
    "acceptance evidence."
)
EDITED_GAP_NOTE = (
    "Use the validated transition playbook, with the Program Manager "
    "accountable for every buyer acceptance gate."
)
INITIAL_COMMITMENT = "Package the browser curation evidence register."
EDITED_COMMITMENT = "Package the approved browser curation evidence register."


@pytest.fixture()
def human_curation_seed(e2e_server: E2EServer):
    """Use the representative graph only inside the disposable E2E root."""
    ledger_path = e2e_server.workspace.artifacts / "llm_calls.jsonl"
    ledger_start = (
        len(ledger_path.read_text(encoding="utf-8").splitlines())
        if ledger_path.exists()
        else 0
    )
    script = (
        PROJECT_ROOT
        / "tests"
        / "e2e"
        / "support"
        / "seed_surface_data.py"
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
    seed_log = e2e_server.workspace.artifacts / "human_curation_seed.log"
    seed_log.write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if result.returncode != 0:
        pytest.fail(
            f"synthetic human-curation seed failed (exit {result.returncode}); "
            f"see {seed_log}",
            pytrace=False,
        )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(
            f"human-curation seed returned invalid JSON: {exc}; see {seed_log}",
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
    cleanup_log = e2e_server.workspace.artifacts / "human_curation_cleanup.log"
    cleanup_log.write_text(
        (cleanup.stdout or "") + (cleanup.stderr or ""), encoding="utf-8"
    )
    if cleanup.returncode != 0:
        pytest.fail(
            f"synthetic human-curation cleanup failed "
            f"(exit {cleanup.returncode}); see {cleanup_log}",
            pytrace=False,
        )


@pytest.fixture()
def human_curation_browser(
    human_curation_seed: dict[str, int],
    browser_session: BrowserSession,
) -> tuple[BrowserSession, dict[str, int]]:
    return browser_session, human_curation_seed


def _row(database_path: Path, sql: str, params: tuple[Any, ...] = ()):
    with sqlite3.connect(database_path, timeout=2.0) as db:
        return db.execute(sql, params).fetchone()


def _eventually(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    description: str,
    timeout: float = 10.0,
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
        except (OSError, ValueError, json.JSONDecodeError, sqlite3.OperationalError) as exc:
            last_error = exc
        time.sleep(0.05)
    detail = f"last value={last_value!r}"
    if last_error is not None:
        detail += f", last error={last_error!r}"
    raise AssertionError(f"Timed out waiting for {description}; {detail}")


def _wait_for_row(
    database_path: Path,
    sql: str,
    params: tuple[Any, ...],
    predicate: Callable[[Any], bool],
    *,
    description: str,
) -> Any:
    return _eventually(
        lambda: _row(database_path, sql, params),
        predicate,
        description=description,
    )


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _goto(page, url: str, expected_text: str) -> None:
    response = page.goto(url, wait_until="domcontentloaded")
    assert response is not None and response.status == 200
    page.get_by_text(expected_text, exact=False).filter(visible=True).first.wait_for(
        state="visible", timeout=15_000
    )


def _proposal_tab(page, label: str):
    label_node = page.locator(".q-tab__label").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    )
    return page.get_by_role("tab").filter(has=label_node).first


def _open_proposal_tab(page, label: str, expected_text: str) -> None:
    tab = _proposal_tab(page, label)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true", timeout=10_000)
    page.get_by_text(expected_text, exact=False).filter(visible=True).first.wait_for(
        state="visible", timeout=10_000
    )


def _card_with_text(page, text: str):
    return page.locator(".q-card").filter(has_text=text).first


def test_human_curated_decisions_commitments_and_timeline(
    human_curation_browser: tuple[BrowserSession, dict[str, int]],
) -> None:
    session, seed = human_curation_browser
    page = session.page
    base_url = session.server.base_url
    database_path = session.server.workspace.database_path
    data_root = session.server.workspace.root
    proposal_id = int(seed["it_proposal_id"])
    gap_pk = int(seed["gap_id"])
    requirement_pk = int(seed["it_requirement_id"])
    decisions_path = data_root / "decisions.json"

    # Gap notes are proposal-local, while the optional remembered decision is
    # durable cross-RFP JSON. Exercise both boundaries through the same dialog.
    _goto(
        page,
        f"{base_url}/proposals/{proposal_id}",
        IT_TITLE,
    )
    _open_proposal_tab(page, "Gaps", "GAP-001")
    page.get_by_role("button", name="Add notes", exact=True).click()
    notes_dialog = page.get_by_role("dialog").last
    notes_dialog.get_by_text("Resolution notes — GAP-001", exact=True).wait_for()
    notes_dialog.get_by_label("Notes", exact=True).fill(INITIAL_GAP_NOTE)
    notes_dialog.get_by_role(
        "checkbox", name=re.compile(r"Remember this decision")
    ).click()
    notes_dialog.get_by_label(
        "Topic (short name for the decision)", exact=True
    ).fill(DECISION_TOPIC)
    notes_dialog.get_by_role("button", name="Save", exact=True).click()
    notes_dialog.wait_for(state="hidden", timeout=10_000)

    _wait_for_row(
        database_path,
        "SELECT resolution_notes FROM gap_analyses WHERE id = ?",
        (gap_pk,),
        lambda row: row == (INITIAL_GAP_NOTE,),
        description="created gap note",
    )
    decision = _eventually(
        lambda: next(
            (
                item
                for item in (_read_json(decisions_path).get("decisions") or [])
                if item.get("topic") == DECISION_TOPIC
            ),
            None,
        ),
        lambda value: value is not None,
        description="remembered decision JSON entry",
    )
    assert decision == {
        "id": "DEC-002",
        "topic": DECISION_TOPIC,
        "decision": INITIAL_GAP_NOTE,
        "applies_to_gaps_like": (
            "The offeror shall provide a phased modernization approach."
        ),
        "established_on": decision["established_on"],
        "source_proposal_id": proposal_id,
        "source_gap_id": "GAP-001",
    }

    page.get_by_role("button", name="Edit notes", exact=True).click()
    edit_notes_dialog = page.get_by_role("dialog").last
    notes_input = edit_notes_dialog.get_by_label("Notes", exact=True)
    expect(notes_input).to_have_value(INITIAL_GAP_NOTE)
    notes_input.fill(EDITED_GAP_NOTE)
    edit_notes_dialog.get_by_role("button", name="Save", exact=True).click()
    edit_notes_dialog.wait_for(state="hidden", timeout=10_000)
    _wait_for_row(
        database_path,
        "SELECT resolution_notes FROM gap_analyses WHERE id = ?",
        (gap_pk,),
        lambda row: row == (EDITED_GAP_NOTE,),
        description="edited gap note",
    )
    remembered = next(
        item
        for item in _read_json(decisions_path)["decisions"]
        if item["topic"] == DECISION_TOPIC
    )
    assert remembered["decision"] == INITIAL_GAP_NOTE

    # Config > Decisions must expose a confirmation boundary and atomically
    # remove only the selected remembered decision.
    _goto(page, f"{base_url}/config?tab=decisions", DECISION_TOPIC)
    decision_card = _card_with_text(page, DECISION_TOPIC)
    decision_card.locator("button").last.click()
    delete_decision_dialog = page.get_by_role("dialog").last
    delete_decision_dialog.get_by_text(
        "Delete decision DEC-002?", exact=True
    ).wait_for()
    delete_decision_dialog.get_by_role(
        "button", name="Delete", exact=True
    ).click()
    delete_decision_dialog.wait_for(state="hidden", timeout=10_000)
    remaining_decisions = _eventually(
        lambda: _read_json(decisions_path).get("decisions") or [],
        lambda rows: all(row.get("topic") != DECISION_TOPIC for row in rows),
        description="confirmed decision deletion",
    )
    assert [row["id"] for row in remaining_decisions] == ["DEC-001"]
    expect(page.get_by_text(DECISION_TOPIC, exact=True)).to_have_count(0)

    # Submission Checklist: prove the matrix hide filter cannot suppress the
    # independent commitment section, then exercise full manual CRUD.
    _goto(
        page,
        f"{base_url}/proposals/{proposal_id}",
        IT_TITLE,
    )
    _open_proposal_tab(
        page,
        "Submission Checklist",
        "Submit the signed synthetic representations form.",
    )
    requirement_card = _card_with_text(
        page, "Submit the signed synthetic representations form."
    )
    requirement_card.get_by_role("checkbox").click()
    _wait_for_row(
        database_path,
        "SELECT submission_obtained FROM compliance_matrix_items WHERE id = ?",
        (requirement_pk,),
        lambda row: row == (1,),
        description="matrix checklist item obtained",
    )
    requirement_card = _card_with_text(
        page, "Submit the signed synthetic representations form."
    )
    expect(requirement_card.get_by_role("checkbox")).to_be_checked(
        timeout=10_000
    )
    page.get_by_role("button", name="Hide obtained", exact=True).click()
    page.get_by_role("button", name="Show all", exact=True).wait_for()
    page.get_by_text("Drafting commitments", exact=True).wait_for()
    expect(
        page.get_by_role("button", name="Add commitment", exact=True)
    ).to_be_visible()

    page.get_by_role("button", name="Add commitment", exact=True).click()
    add_commitment_dialog = page.get_by_role("dialog").last
    add_commitment_dialog.get_by_text("Add commitment", exact=True).wait_for()
    add_commitment_dialog.get_by_label("Commitment", exact=True).fill(
        INITIAL_COMMITMENT
    )
    add_commitment_dialog.get_by_label("Notes (optional)", exact=True).fill(
        "Owner: browser QA lead"
    )
    add_commitment_dialog.get_by_role(
        "button", name="Save", exact=True
    ).click()
    add_commitment_dialog.wait_for(state="hidden", timeout=10_000)
    created_commitment = _wait_for_row(
        database_path,
        "SELECT id, description, source, source_section_id, obtained, notes "
        "FROM submission_commitments WHERE proposal_id = ? "
        "AND description = ?",
        (proposal_id, INITIAL_COMMITMENT),
        lambda row: row is not None,
        description="manual commitment creation",
    )
    commitment_id = int(created_commitment[0])
    assert created_commitment[1:] == (
        INITIAL_COMMITMENT,
        "manual",
        None,
        0,
        "Owner: browser QA lead",
    )

    commitment_card = _card_with_text(page, INITIAL_COMMITMENT)
    commitment_card.get_by_role("button", name="Edit", exact=True).click()
    edit_commitment_dialog = page.get_by_role("dialog").last
    edit_commitment_dialog.get_by_text("Edit commitment", exact=True).wait_for()
    edit_commitment_dialog.get_by_label("Commitment", exact=True).fill(
        EDITED_COMMITMENT
    )
    edit_commitment_dialog.get_by_label("Notes (optional)", exact=True).fill(
        "Final owner: proposal manager"
    )
    edit_commitment_dialog.get_by_role(
        "button", name="Save", exact=True
    ).click()
    edit_commitment_dialog.wait_for(state="hidden", timeout=10_000)
    _wait_for_row(
        database_path,
        "SELECT description, source, source_section_id, obtained, notes "
        "FROM submission_commitments WHERE id = ?",
        (commitment_id,),
        lambda row: row
        == (
            EDITED_COMMITMENT,
            "manual",
            None,
            0,
            "Final owner: proposal manager",
        ),
        description="manual commitment edit",
    )
    page.get_by_text(EDITED_COMMITMENT, exact=True).wait_for()

    commitment_card = _card_with_text(page, EDITED_COMMITMENT)
    commitment_card.get_by_role("checkbox").click()
    _wait_for_row(
        database_path,
        "SELECT obtained FROM submission_commitments WHERE id = ?",
        (commitment_id,),
        lambda row: row == (1,),
        description="manual commitment obtained toggle",
    )
    expect(page.get_by_text(EDITED_COMMITMENT, exact=True)).to_have_count(0)
    page.get_by_role("button", name="Show all", exact=True).click()
    page.get_by_text(EDITED_COMMITMENT, exact=True).wait_for()

    commitment_card = _card_with_text(page, EDITED_COMMITMENT)
    commitment_card.get_by_role("button", name="Remove", exact=True).click()
    remove_dialog = page.get_by_role("dialog").last
    remove_dialog.get_by_text("Remove commitment?", exact=True).wait_for()
    remove_dialog.get_by_role("button", name="Remove", exact=True).click()
    remove_dialog.wait_for(state="hidden", timeout=10_000)
    _wait_for_row(
        database_path,
        "SELECT id FROM submission_commitments WHERE id = ?",
        (commitment_id,),
        lambda row: row is None,
        description="manual commitment deletion",
    )
    expect(page.get_by_text(EDITED_COMMITMENT, exact=True)).to_have_count(0)

    # Timeline: change/clear the anchor, edit the seeded phase without changing
    # its identity, then confirm-delete it. Add is covered by the surface test.
    _open_proposal_tab(page, "Timeline", "Seeded Discovery")
    anchor_input = page.get_by_label("Anchor date", exact=True)
    expect(anchor_input).to_have_value("2030-10-15")
    anchor_input.fill("2031-01-05")
    page.get_by_role(
        "button", name="Apply anchor date", exact=True
    ).click()
    anchored_timeline = _wait_for_row(
        database_path,
        "SELECT timeline_json FROM proposals WHERE id = ?",
        (proposal_id,),
        lambda row: bool(
            row and json.loads(row[0] or "{}").get("anchor_date") == "2031-01-05"
        ),
        description="timeline anchor update",
    )
    assert json.loads(anchored_timeline[0])["phases"][0]["id"] == (
        "phase-seeded-discovery"
    )
    page.get_by_text("Jan 05, 2031", exact=False).first.wait_for()

    phase_card = page.locator(".q-card").filter(
        has_text="Seeded Discovery"
    ).last
    phase_card.get_by_role("button", name="Edit phase", exact=True).click()
    phase_dialog = page.get_by_role("dialog").last
    phase_dialog.get_by_text("Edit Phase", exact=True).wait_for()
    phase_dialog.get_by_label("Phase name", exact=True).fill(
        "Curated Discovery"
    )
    phase_dialog.get_by_label("Start offset (days)", exact=True).fill("5")
    phase_dialog.get_by_label("Duration (days)", exact=True).fill("45")
    phase_dialog.get_by_label("Deliverable / output", exact=True).fill(
        "Approved discovery evidence register"
    )
    phase_dialog.get_by_label("Owner / role (optional)", exact=True).fill(
        "Proposal Manager"
    )
    phase_dialog.get_by_role("button", name="Save", exact=True).click()
    phase_dialog.wait_for(state="hidden", timeout=10_000)
    edited_timeline_row = _wait_for_row(
        database_path,
        "SELECT timeline_json FROM proposals WHERE id = ?",
        (proposal_id,),
        lambda row: bool(row and "Curated Discovery" in (row[0] or "")),
        description="timeline phase edit",
    )
    edited_timeline = json.loads(edited_timeline_row[0])
    assert edited_timeline["anchor_date"] == "2031-01-05"
    assert edited_timeline["phases"] == [
        {
            "id": "phase-seeded-discovery",
            "phase_name": "Curated Discovery",
            "start_offset": 5,
            "duration": 45,
            "deliverable": "Approved discovery evidence register",
            "owner": "Proposal Manager",
            "color": "#1F3A5F",
            "order": 0,
        }
    ]
    page.get_by_text("Curated Discovery", exact=True).first.wait_for()

    phase_card = page.locator(".q-card").filter(
        has_text="Curated Discovery"
    ).last
    phase_card.get_by_role("button", name="Delete phase", exact=True).click()
    delete_phase_dialog = page.get_by_role("dialog").last
    delete_phase_dialog.get_by_text("Delete phase?", exact=True).wait_for()
    delete_phase_dialog.get_by_role(
        "button", name="Delete", exact=True
    ).click()
    delete_phase_dialog.wait_for(state="hidden", timeout=10_000)
    deleted_phase_timeline = _wait_for_row(
        database_path,
        "SELECT timeline_json FROM proposals WHERE id = ?",
        (proposal_id,),
        lambda row: bool(
            row and json.loads(row[0] or "{}").get("phases") == []
        ),
        description="timeline phase deletion",
    )
    assert json.loads(deleted_phase_timeline[0])["anchor_date"] == "2031-01-05"
    expect(page.get_by_text("Curated Discovery", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Add Phase", exact=True)).to_be_visible()

    page.get_by_role(
        "button", name="Clear anchor date", exact=True
    ).click()
    cleared_timeline = _wait_for_row(
        database_path,
        "SELECT timeline_json FROM proposals WHERE id = ?",
        (proposal_id,),
        lambda row: bool(
            row and json.loads(row[0] or "{}").get("anchor_date") is None
        ),
        description="timeline anchor clear",
    )
    assert json.loads(cleared_timeline[0])["phases"] == []

    # These workflows are deterministic human curation and must never cross a
    # provider boundary.
    ledger_path = session.server.workspace.artifacts / "llm_calls.jsonl"
    ledger_lines = (
        ledger_path.read_text(encoding="utf-8").splitlines()
        if ledger_path.exists()
        else []
    )
    assert len(ledger_lines) == int(seed["ledger_start"])
