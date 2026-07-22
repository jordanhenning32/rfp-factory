"""Real-browser proof of the payment-systems workflow through archive."""
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
from playwright.sync_api import expect

from tests.e2e.conftest import BrowserSession, E2EServer

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TITLE = "Synthetic Payment Workflow RFP"

EXPECTED_PAYMENT_CALLS = Counter(
    {
        (
            "complete_with_search",
            "payment_market_researcher_grounded",
            None,
        ): 1,
        (
            "call_tool",
            "payment_market_researcher_structure",
            "report_payment_market_scan",
        ): 1,
        (
            "complete_with_web_search",
            "payment_market_researcher_b_grounded",
            None,
        ): 1,
        (
            "call_tool",
            "payment_market_researcher_b_structure",
            "report_payment_market_scan",
        ): 1,
        (
            "call_tool",
            "cost_writer",
            "draft_cost_section",
        ): 1,
        (
            "call_tool",
            "payment_cost_reviewer",
            "report_payment_cost_review",
        ): 1,
        (
            "call_tool",
            "writer_team",
            "report_section_draft",
        ): 1,
        ("call_tool", "reviewer_a", "report_findings"): 2,
        ("call_tool", "reviewer_b", "report_findings"): 2,
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
def payment_seed(e2e_server: E2EServer):
    ledger_path = e2e_server.workspace.artifacts / "llm_calls.jsonl"
    ledger_start = (
        len(ledger_path.read_text(encoding="utf-8").splitlines())
        if ledger_path.exists()
        else 0
    )
    script = PROJECT_ROOT / "tests" / "e2e" / "support" / "seed_payment_data.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        env=e2e_server.workspace.environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    log_path = e2e_server.workspace.artifacts / "payment_seed.log"
    log_path.write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if result.returncode != 0:
        pytest.fail(
            f"synthetic payment seed failed (exit {result.returncode}); "
            f"see {log_path}",
            pytrace=False,
        )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(
            f"payment seed returned invalid JSON: {exc}; see {log_path}",
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
    cleanup_path = e2e_server.workspace.artifacts / "payment_cleanup.log"
    cleanup_path.write_text(
        (cleanup.stdout or "") + (cleanup.stderr or ""), encoding="utf-8"
    )
    if cleanup.returncode != 0:
        pytest.fail(
            f"synthetic payment cleanup failed (exit {cleanup.returncode}); "
            f"see {cleanup_path}",
            pytrace=False,
        )


@pytest.fixture()
def payment_browser(
    payment_seed: dict[str, int],
    browser_session: BrowserSession,
) -> tuple[BrowserSession, dict[str, int]]:
    return browser_session, payment_seed


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
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f"last value={last_value!r}"
    if last_error is not None:
        detail += f", last error={last_error!r}"
    raise AssertionError(f"Timed out waiting for {description}; {detail}")


def _wait_for_status(
    database_path: Path,
    proposal_id: int,
    expected: str,
) -> sqlite3.Row:
    row = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT id, status, submitted_at FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda value: value is not None and value["status"] == expected,
        description=f"proposal #{proposal_id} status={expected!r}",
    )
    assert isinstance(row, sqlite3.Row)
    return row


def _goto_proposal(page, base_url: str, proposal_id: int) -> None:
    response = page.goto(
        f"{base_url}/proposals/{proposal_id}",
        wait_until="domcontentloaded",
    )
    assert response is not None and response.status == 200
    page.get_by_text(TITLE, exact=True).wait_for(state="visible", timeout=15_000)


def _proposal_tab(page, label: str):
    label_node = page.locator(".q-tab__label").filter(
        has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)
    )
    return page.get_by_role("tab").filter(has=label_node).first


def _open_tab(page, label: str) -> None:
    tab = _proposal_tab(page, label)
    tab.wait_for(state="visible", timeout=10_000)
    tab.click()
    expect(tab).to_have_attribute("aria-selected", "true", timeout=10_000)


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


def _scan_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None or not (row["payment_market_scan_json"] or "").strip():
        return {}
    return json.loads(row["payment_market_scan_json"])


def test_payment_workflow_through_archive(
    payment_browser: tuple[BrowserSession, dict[str, int]],
) -> None:
    session, seed = payment_browser
    page = session.page
    base_url = session.server.base_url
    database_path = session.server.workspace.database_path
    data_root = session.server.workspace.root
    proposal_id = int(seed["proposal_id"])

    _wait_for_status(database_path, proposal_id, "awaiting_cost_build")
    _goto_proposal(page, base_url, proposal_id)
    page.get_by_text(
        "Action needed: run Payment Market Research", exact=True
    ).wait_for(state="visible", timeout=10_000)
    page.get_by_text(
        "the payment-specific Cost Reviewer then fact-checks the narrative",
        exact=False,
    ).wait_for(state="visible", timeout=10_000)
    _open_tab(page, "Cost")
    page.get_by_role(
        "button", name="Run Payment Market Research", exact=True
    ).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )

    scan = _eventually(
        lambda: _scan_from_row(
            _fetchone(
                database_path,
                "SELECT payment_market_scan_json FROM proposals WHERE id = ?",
                (proposal_id,),
            )
        ),
        lambda value: bool(
            value.get("pricing_structure")
            and value.get("comparable_awards")
            and value.get("competitor_processors")
            and value.get("profit_math")
        ),
        description="persisted dual-provider payment market scan",
    )
    assert scan["pricing_structure"]["pricing_model"] == "interchange_plus"
    assert scan["pricing_structure"]["proposed_credit_card_markup_bps"] == 20
    assert scan["volume_estimate"]["annual_processed_volume_midpoint_usd"] == 20_000_000
    assert {
        tuple(award["confirmed_by"])
        for award in scan["comparable_awards"]
    } == {("gemini", "claude")}
    profit_before = scan["profit_math"]["annual_net_profit_midpoint_usd"]
    assert profit_before is not None

    # The payment cost basis is a real product-level data file. Edit it through
    # the dialog and prove both the file and the persisted proposal math update.
    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost")
    page.get_by_text("Payment Market Scan", exact=True).wait_for()
    page.get_by_role("button", name="Edit Cost Basis", exact=True).click()
    dialog = page.get_by_role("dialog").last
    dialog.get_by_text("Edit Cost Basis", exact=True).wait_for()
    dialog.get_by_label(
        "Sponsor / acquirer fee (basis points)", exact=True
    ).fill("5")
    dialog.get_by_label(
        "Gateway / network access per transaction (USD)", exact=True
    ).fill("0.01")
    dialog.get_by_label(
        "Annualized PCI compliance cost (USD/year)", exact=True
    ).fill("1000")
    dialog.get_by_label(
        "Annualized support allocation per client (USD/year)", exact=True
    ).fill("2000")
    confirmed = dialog.get_by_role(
        "checkbox", name="Confirmed by ops finance", exact=True
    )
    if not confirmed.is_checked():
        confirmed.click()
    dialog.get_by_role("button", name="Save", exact=True).click()
    dialog.wait_for(state="hidden", timeout=15_000)

    pricing_path = data_root / "pricing" / "payment_systems.json"
    cost_basis = _eventually(
        lambda: json.loads(pricing_path.read_text(encoding="utf-8")).get(
            "our_cost_basis", {}
        ),
        lambda value: bool(
            value.get("_confirmed_by_ops_finance")
            and value.get("sponsor_acquirer_fee_bps") == 5.0
            and value.get("gateway_per_txn_usd") == 0.01
        ),
        description="saved payment cost basis",
    )
    assert cost_basis["annualized_pci_compliance_usd"] == 1000.0
    assert cost_basis["annualized_support_allocation_usd"] == 2000.0
    recomputed_scan = _eventually(
        lambda: _scan_from_row(
            _fetchone(
                database_path,
                "SELECT payment_market_scan_json FROM proposals WHERE id = ?",
                (proposal_id,),
            )
        ),
        lambda value: bool(
            value.get("profit_math", {}).get("annual_net_profit_midpoint_usd")
            != profit_before
            and value.get("profit_math", {}).get("cost_basis_assumptions") == []
        ),
        description="profit math recomputed from confirmed cost basis",
    )
    assert (
        recomputed_scan["profit_math"]["annual_net_profit_midpoint_usd"]
        > profit_before
    )

    # Persist an explicit user pricing-model choice before drafting.
    page.get_by_role("button", name="Flat rate", exact=True).filter(
        visible=True
    ).click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT selected_pricing_model FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None and row[0] == "flat_rate",
        description="selected payment pricing model",
    )

    page.get_by_role(
        "button", name="Run Cost Volume Writer", exact=True
    ).last.click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    _eventually(
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
        description="persisted payment Cost Writer draft",
    )
    _wait_for_status(database_path, proposal_id, "awaiting_draft")

    # Payment systems uses its own JSON-backed adversarial reviewer rather
    # than the labor-scenario reviewer.
    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost Review")
    page.get_by_role("button", name="Run Cost Reviewer", exact=True).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    payment_review = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT payment_cost_review_findings_json FROM proposals WHERE id = ?",
            (proposal_id,),
        ),
        lambda row: bool(row and (row[0] or "").strip()),
        description="persisted payment Cost Reviewer result",
    )
    review_data = json.loads(payment_review[0])
    assert review_data["bid_ready"] is True
    assert review_data["findings"] == []
    assert review_data["sections_reviewed"] == ["SEC-103"]

    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role("button", name="Begin Drafting", exact=True).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT draft_text_markdown, current_revision_number "
            "FROM proposal_sections WHERE proposal_id = ? "
            "AND section_id = 'SEC-101'",
            (proposal_id,),
        ),
        lambda row: row is not None
        and bool(row["draft_text_markdown"])
        and row["current_revision_number"] == 1,
        description="persisted payment narrative Writer Team draft",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")

    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role(
        "button", name="Run Auto Review-Revise Loop", exact=True
    ).click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT "
            "SUM(CASE WHEN agent_name = 'reviewer_a' AND status = 'completed' "
            "THEN 1 ELSE 0 END) AS a_runs, "
            "SUM(CASE WHEN agent_name = 'reviewer_b' AND status = 'completed' "
            "THEN 1 ELSE 0 END) AS b_runs "
            "FROM agent_runs WHERE proposal_id = ?",
            (proposal_id,),
        ),
        lambda row: row is not None
        and row["a_runs"] == 1
        and row["b_runs"] == 1,
        description="clean payment proposal reviewer passes",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")

    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Final Polish")
    page.get_by_role("button", name="Run Final Polish", exact=True).click()
    _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT section_id_label, issue_type FROM polish_edits "
            "WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ),
        lambda row: row is not None
        and row["section_id_label"] == "SEC-101"
        and row["issue_type"] == "numerical_drift",
        description="payment workflow Final Polish edit",
    )

    polished_section = _eventually(
        lambda: _fetchone(
            database_path,
            "SELECT id, current_revision_number FROM proposal_sections "
            "WHERE proposal_id = ? AND section_id = 'SEC-101'",
            (proposal_id,),
        ),
        lambda row: row is not None and row["current_revision_number"] == 2,
        description="payment narrative revision after Final Polish",
    )

    # Final Polish changed the narrative after its first clean A/B pass. A
    # second real pass produces current-revision coverage before approval.
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
        description="payment current-revision review after Final Polish",
    )
    _wait_for_status(database_path, proposal_id, "draft_ready")

    # The shared readiness gate now proves the payment scan and payment review
    # are accepted substitutes for the IT labor packages/reviewer pass.
    _goto_proposal(page, base_url, proposal_id)
    page.get_by_role(
        "button", name="Approve for submission", exact=True
    ).click()
    _wait_for_status(database_path, proposal_id, "approved")
    page.get_by_role("button", name="Mark as submitted", exact=True).click()
    submit_dialog = page.get_by_role("dialog").last
    submit_dialog.get_by_role(
        "button", name="Confirm submitted", exact=True
    ).click()
    submitted = _wait_for_status(database_path, proposal_id, "submitted")
    assert submitted["submitted_at"] is not None
    page.get_by_role("button", name="Archive proposal", exact=True).click()
    archive_dialog = page.get_by_role("dialog").last
    archive_dialog.get_by_role("button", name="Archive", exact=True).click()
    _wait_for_status(database_path, proposal_id, "archived")
    page.get_by_text("Archived (read-only)", exact=True).wait_for(
        state="visible", timeout=10_000
    )
    _open_tab(page, "Cost Review")
    expect(
        page.get_by_role("button", name="Re-run Cost Reviewer", exact=True)
    ).to_be_disabled()

    ledger_path = session.server.workspace.artifacts / "llm_calls.jsonl"
    assert ledger_path.is_file()
    assert _new_ledger_calls(
        ledger_path, int(seed["ledger_start"])
    ) == EXPECTED_PAYMENT_CALLS
