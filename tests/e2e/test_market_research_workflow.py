"""Real-browser proof of the IT-services dual-provider market scan."""
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
TITLE = "Synthetic IT Market Research RFP"

EXPECTED_CALLS = Counter(
    {
        ("complete_with_search", "market_researcher_grounded", None): 1,
        (
            "call_tool",
            "market_researcher_structure",
            "report_market_scan",
        ): 1,
        (
            "complete_with_web_search",
            "market_researcher_b_grounded",
            None,
        ): 1,
        (
            "call_tool",
            "market_researcher_b_structure",
            "report_market_scan",
        ): 1,
    }
)

EXPECTED_AGENT_NAMES = {
    "market_researcher_grounded",
    "market_researcher_structure",
    "market_researcher_b_grounded",
    "market_researcher_b_structure",
}


@pytest.fixture()
def market_research_seed(e2e_server: E2EServer):
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
        / "seed_market_research_data.py"
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
    log_path = e2e_server.workspace.artifacts / "it_market_seed.log"
    log_path.write_text(
        (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
    )
    if result.returncode != 0:
        pytest.fail(
            f"synthetic IT market seed failed (exit {result.returncode}); "
            f"see {log_path}",
            pytrace=False,
        )
    try:
        payload = json.loads((result.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        pytest.fail(
            f"IT market seed returned invalid JSON: {exc}; see {log_path}",
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
    cleanup_path = e2e_server.workspace.artifacts / "it_market_cleanup.log"
    cleanup_path.write_text(
        (cleanup.stdout or "") + (cleanup.stderr or ""), encoding="utf-8"
    )
    if cleanup.returncode != 0:
        pytest.fail(
            f"synthetic IT market cleanup failed (exit {cleanup.returncode}); "
            f"see {cleanup_path}",
            pytrace=False,
        )


@pytest.fixture()
def market_research_browser(
    market_research_seed: dict[str, int],
    browser_session: BrowserSession,
) -> tuple[BrowserSession, dict[str, int]]:
    return browser_session, market_research_seed


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
        except (OSError, ValueError, sqlite3.OperationalError) as exc:
            last_error = exc
        time.sleep(0.1)
    detail = f"last value={last_value!r}"
    if last_error is not None:
        detail += f", last error={last_error!r}"
    raise AssertionError(f"Timed out waiting for {description}; {detail}")


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
    if not path.exists():
        return Counter()
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


def _json_list(raw: str | None) -> list[Any]:
    return json.loads(raw) if raw else []


def _load_market_state(database_path: Path, proposal_id: int) -> dict[str, Any]:
    scan = _fetchone(
        database_path,
        "SELECT id, proposal_id, market_band_low_usd, market_band_mid_usd, "
        "market_band_high_usd, methodology, agent_run_id, created_at, updated_at "
        "FROM market_scans WHERE proposal_id = ?",
        (proposal_id,),
    )
    awards: list[sqlite3.Row] = []
    competitors: list[sqlite3.Row] = []
    if scan is not None:
        awards = _fetchall(
            database_path,
            "SELECT id, award_title, award_value_usd, "
            "period_of_performance_months, awardee_name, customer_agency, "
            "source_url, relevance_score, notes, confirmed_by, needs_review "
            "FROM market_scan_comparable_awards WHERE market_scan_id = ? "
            "ORDER BY id",
            (scan["id"],),
        )
        competitors = _fetchall(
            database_path,
            "SELECT id, competitor_name, likelihood_to_bid, "
            "estimated_rate_low_usd, estimated_rate_high_usd, "
            "rate_estimation_basis, source_urls, notes, confirmed_by, "
            "needs_review FROM market_scan_competitors "
            "WHERE market_scan_id = ? ORDER BY id",
            (scan["id"],),
        )
    runs = _fetchall(
        database_path,
        "SELECT id, agent_name, model_used, input_tokens, output_tokens, "
        "cost_usd, started_at, completed_at, status, error_text "
        "FROM agent_runs WHERE proposal_id = ? AND agent_name IN (?, ?, ?, ?) "
        "ORDER BY id",
        (
            proposal_id,
            "market_researcher_grounded",
            "market_researcher_structure",
            "market_researcher_b_grounded",
            "market_researcher_b_structure",
        ),
    )
    return {
        "scan": scan,
        "awards": awards,
        "competitors": competitors,
        "runs": runs,
    }


def _assert_persisted_scan(state: dict[str, Any]) -> None:
    scan = state["scan"]
    assert scan is not None
    assert float(scan["market_band_low_usd"]) == 900_000
    assert float(scan["market_band_mid_usd"]) == 1_800_000
    assert float(scan["market_band_high_usd"]) == 2_400_000
    assert scan["agent_run_id"] is None

    methodology = scan["methodology"] or ""
    assert (
        "Consolidator recalculated the persisted market band from 4 cited "
        "comparable award value(s) normalized to 12 month(s)."
    ) in methodology
    assert "[Gemini grounded] Gemini compared cited synthetic" in methodology
    assert "[Claude+web] Claude independently compared cited synthetic" in methodology
    assert (
        "Provider-reported band before evidence recalculation: "
        "low=$1,050,000 / mid=$2,250,000 / high=$4,900,000."
    ) in methodology
    assert "WARNING:" not in methodology

    awards = {row["award_title"]: row for row in state["awards"]}
    assert set(awards) == {
        "Synthetic Browser Modernization Task Order 2025",
        "Synthetic Cloud Delivery Task Order 2025",
        "Synthetic Legacy Platform Support Award",
        "Synthetic Records Transformation Award",
    }
    browser = awards["Synthetic Browser Modernization Task Order 2025"]
    assert float(browser["award_value_usd"]) == 1_200_000
    assert browser["period_of_performance_months"] == 12
    assert browser["awardee_name"] == "Acme Federal Solutions LLC"
    assert browser["customer_agency"] == "E2E Digital Services Agency"
    assert browser["source_url"] == (
        "https://gemini-market.invalid/browser-modernization"
    )
    assert float(browser["relevance_score"]) == 0.95
    assert _json_list(browser["confirmed_by"]) == ["gemini", "claude"]
    assert browser["needs_review"] == 0

    cloud = awards["Synthetic Cloud Delivery Task Order 2025"]
    assert float(cloud["award_value_usd"]) == 4_800_000
    assert cloud["period_of_performance_months"] == 24
    assert cloud["source_url"] == "https://gemini-market.invalid/cloud-delivery"
    assert _json_list(cloud["confirmed_by"]) == ["gemini", "claude"]
    assert cloud["needs_review"] == 0

    gemini_only = awards["Synthetic Legacy Platform Support Award"]
    assert float(gemini_only["award_value_usd"]) == 900_000
    assert float(gemini_only["relevance_score"]) == 0.65
    assert gemini_only["source_url"] == (
        "https://gemini-market.invalid/legacy-platform"
    )
    assert _json_list(gemini_only["confirmed_by"]) == ["gemini"]
    assert gemini_only["needs_review"] == 1

    claude_only = awards["Synthetic Records Transformation Award"]
    assert float(claude_only["award_value_usd"]) == 3_600_000
    assert claude_only["period_of_performance_months"] == 18
    assert float(claude_only["relevance_score"]) == 0.8
    assert claude_only["source_url"] == (
        "https://claude-market.invalid/records-transformation"
    )
    assert _json_list(claude_only["confirmed_by"]) == ["claude"]
    assert claude_only["needs_review"] == 0

    competitors = {row["competitor_name"]: row for row in state["competitors"]}
    assert set(competitors) == {
        "Acme Federal Solutions LLC",
        "Gemini Only Digital LLC",
        "Claude Only Dynamics Inc.",
    }
    acme = competitors["Acme Federal Solutions LLC"]
    assert acme["likelihood_to_bid"] == "high"
    assert float(acme["estimated_rate_low_usd"]) == 155
    assert float(acme["estimated_rate_high_usd"]) == 195
    assert set(_json_list(acme["source_urls"])) == {
        "https://gemini-market.invalid/acme-rate",
        "https://claude-market.invalid/acme-rate",
    }
    assert _json_list(acme["confirmed_by"]) == ["gemini", "claude"]
    assert acme["needs_review"] == 0

    gemini_competitor = competitors["Gemini Only Digital LLC"]
    assert float(gemini_competitor["estimated_rate_low_usd"]) == 135
    assert float(gemini_competitor["estimated_rate_high_usd"]) == 175
    assert _json_list(gemini_competitor["source_urls"]) == [
        "https://gemini-market.invalid/gemini-only-rate"
    ]
    assert _json_list(gemini_competitor["confirmed_by"]) == ["gemini"]
    assert gemini_competitor["needs_review"] == 1

    claude_competitor = competitors["Claude Only Dynamics Inc."]
    assert float(claude_competitor["estimated_rate_low_usd"]) == 145
    assert float(claude_competitor["estimated_rate_high_usd"]) == 185
    assert _json_list(claude_competitor["source_urls"]) == [
        "https://claude-market.invalid/claude-only-rate"
    ]
    assert _json_list(claude_competitor["confirmed_by"]) == ["claude"]
    assert claude_competitor["needs_review"] == 1


def _assert_agent_runs(state: dict[str, Any], *, runs_per_agent: int) -> None:
    runs = state["runs"]
    assert len(runs) == 4 * runs_per_agent
    assert Counter(row["agent_name"] for row in runs) == Counter(
        {name: runs_per_agent for name in EXPECTED_AGENT_NAMES}
    )
    for row in runs:
        assert row["model_used"]
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 25
        assert float(row["cost_usd"]) == 0
        assert row["started_at"] is not None
        assert row["completed_at"] is not None
        assert row["status"] == "completed"
        assert row["error_text"] is None


def _assert_scan_ui(page) -> None:
    page.get_by_text("Market scan", exact=True).wait_for(
        state="visible", timeout=10_000
    )
    for amount in ("$900,000", "$1,800,000", "$2,400,000"):
        expect(page.get_by_text(amount, exact=True).first).to_be_visible()

    page.get_by_text("How the band was derived", exact=True).click()
    page.get_by_text(
        re.compile(
            r"Consolidator recalculated the persisted market band from 4 "
            r"cited comparable award value\(s\) normalized to 12 month\(s\)\."
        )
    ).wait_for(state="visible", timeout=10_000)

    awards_panel = page.locator(".q-expansion-item").filter(
        has=page.get_by_text("Comparable awards (4)", exact=True)
    ).first
    awards_panel.get_by_text("Comparable awards (4)", exact=True).click()
    for label in (
        "CONSENSUS · 2",
        "Gemini only · 1",
        "Claude only · 1",
        "Verify · 1",
    ):
        expect(awards_panel.get_by_text(label, exact=True)).to_be_visible()
    for title in (
        "Synthetic Browser Modernization Task Order 2025",
        "Synthetic Cloud Delivery Task Order 2025",
        "Synthetic Legacy Platform Support Award",
        "Synthetic Records Transformation Award",
    ):
        expect(awards_panel.get_by_text(title, exact=True)).to_be_visible()
    for url in (
        "https://gemini-market.invalid/browser-modernization",
        "https://gemini-market.invalid/cloud-delivery",
        "https://gemini-market.invalid/legacy-platform",
        "https://claude-market.invalid/records-transformation",
    ):
        expect(awards_panel.locator(f'a[href="{url}"]')).to_have_count(1)

    competitors_panel = page.locator(".q-expansion-item").filter(
        has=page.get_by_text("Likely competitors (3)", exact=True)
    ).first
    competitors_panel.get_by_text("Likely competitors (3)", exact=True).click()
    for label in (
        "CONSENSUS · 1",
        "Gemini only · 1",
        "Claude only · 1",
        "Verify · 2",
    ):
        expect(competitors_panel.get_by_text(label, exact=True)).to_be_visible()
    acme_row = competitors_panel.locator(
        "tr", has_text="Acme Federal Solutions LLC"
    )
    expect(acme_row).to_contain_text("$155/hr")
    expect(acme_row).to_contain_text("$195/hr")
    expect(acme_row).to_contain_text("Both")
    expect(
        competitors_panel.locator("tr", has_text="Gemini Only Digital LLC")
    ).to_contain_text("Gemini")
    expect(
        competitors_panel.locator("tr", has_text="Claude Only Dynamics Inc.")
    ).to_contain_text("Claude")

    competitors_panel.get_by_text("Competitor source URLs", exact=True).click()
    for url in (
        "https://gemini-market.invalid/acme-rate",
        "https://claude-market.invalid/acme-rate",
        "https://gemini-market.invalid/gemini-only-rate",
        "https://claude-market.invalid/claude-only-rate",
    ):
        expect(competitors_panel.locator(f'a[href="{url}"]')).to_have_count(1)


def test_it_market_research_persists_renders_and_reruns(
    market_research_browser: tuple[BrowserSession, dict[str, int]],
) -> None:
    session, seed = market_research_browser
    page = session.page
    base_url = session.server.base_url
    database_path = session.server.workspace.database_path
    proposal_id = int(seed["proposal_id"])

    proposal = _fetchone(
        database_path,
        "SELECT status, service_line, team_approved_at FROM proposals WHERE id = ?",
        (proposal_id,),
    )
    assert proposal is not None
    assert proposal["status"] == "awaiting_cost_build"
    assert proposal["service_line"] == "it_services"
    assert proposal["team_approved_at"] is not None
    assert _fetchone(
        database_path,
        "SELECT id FROM proposal_team_members WHERE proposal_id = ?",
        (proposal_id,),
    ) is not None
    assert _load_market_state(database_path, proposal_id)["scan"] is None

    _goto_proposal(page, base_url, proposal_id)
    page.get_by_text(
        "Action needed: run the Cost Analyst", exact=True
    ).wait_for(state="visible", timeout=10_000)
    _open_tab(page, "Cost")
    run_market = page.get_by_role(
        "button", name="Run Market Research", exact=True
    )
    expect(run_market).to_be_enabled()
    expect(
        page.get_by_role("button", name="Run Cost Analyst", exact=True)
    ).to_be_disabled()
    run_market.click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )

    first_state = _eventually(
        lambda: _load_market_state(database_path, proposal_id),
        lambda value: bool(
            value["scan"] is not None
            and len(value["awards"]) == 4
            and len(value["competitors"]) == 3
            and len(value["runs"]) == 4
            and all(row["status"] == "completed" for row in value["runs"])
        ),
        description="first persisted dual-provider IT market scan",
    )
    _assert_persisted_scan(first_state)
    _assert_agent_runs(first_state, runs_per_agent=1)

    ledger_path = session.server.workspace.artifacts / "llm_calls.jsonl"
    assert _new_ledger_calls(
        ledger_path, int(seed["ledger_start"])
    ) == EXPECTED_CALLS

    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost")
    _assert_scan_ui(page)
    expect(
        page.get_by_role("button", name="Re-run Market Research", exact=True)
    ).to_be_enabled()
    expect(
        page.get_by_role("button", name="Run Cost Analyst", exact=True)
    ).to_be_enabled()

    first_updated_at = first_state["scan"]["updated_at"]
    page.get_by_role(
        "button", name="Re-run Market Research", exact=True
    ).click()
    page.wait_for_url(
        f"{base_url}/proposals/{proposal_id}/progress", timeout=10_000
    )
    second_state = _eventually(
        lambda: _load_market_state(database_path, proposal_id),
        lambda value: bool(
            value["scan"] is not None
            and value["scan"]["updated_at"] != first_updated_at
            and len(value["awards"]) == 4
            and len(value["competitors"]) == 3
            and len(value["runs"]) == 8
            and all(row["status"] == "completed" for row in value["runs"])
        ),
        description="replacement IT market scan after UI re-run",
    )
    _assert_persisted_scan(second_state)
    _assert_agent_runs(second_state, runs_per_agent=2)
    assert _fetchone(
        database_path,
        "SELECT COUNT(*) AS n FROM market_scans WHERE proposal_id = ?",
        (proposal_id,),
    )["n"] == 1
    assert _fetchone(
        database_path,
        "SELECT COUNT(*) AS n FROM market_scan_comparable_awards "
        "WHERE market_scan_id = ?",
        (second_state["scan"]["id"],),
    )["n"] == 4
    assert _fetchone(
        database_path,
        "SELECT COUNT(*) AS n FROM market_scan_competitors "
        "WHERE market_scan_id = ?",
        (second_state["scan"]["id"],),
    )["n"] == 3
    assert _new_ledger_calls(
        ledger_path, int(seed["ledger_start"])
    ) == Counter({key: 2 for key in EXPECTED_CALLS})

    _goto_proposal(page, base_url, proposal_id)
    _open_tab(page, "Cost")
    page.get_by_text("Market scan", exact=True).wait_for(
        state="visible", timeout=10_000
    )
    expect(
        page.get_by_role("button", name="Re-run Market Research", exact=True)
    ).to_be_enabled()
    expect(
        page.get_by_role("button", name="Run Cost Analyst", exact=True)
    ).to_be_enabled()
