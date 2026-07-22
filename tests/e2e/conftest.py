"""Fixtures for isolated real-browser end-to-end tests.

No production ``app`` module is imported in this pytest process. Paths and
settings in the application are module-level, so the only reliable isolation
boundary is a fresh migrated database and a fresh application subprocess for
every E2E pytest invocation.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GROK_API_KEY",
    "VOYAGE_API_KEY",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--e2e",
        action="store_true",
        default=False,
        help="Run isolated Playwright end-to-end tests.",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture(scope="session")
def e2e_enabled(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--e2e"):
        pytest.skip("real-browser tests are disabled; pass --e2e to enable")


@dataclass(frozen=True)
class E2EWorkspace:
    root: Path
    database_path: Path
    artifacts: Path
    environment: dict[str, str]


@dataclass
class E2EServer:
    base_url: str
    workspace: E2EWorkspace
    process: subprocess.Popen[str]
    server_log: Path


@dataclass
class BrowserSession:
    page: Any
    issues: list[str]
    artifacts: Path
    server: E2EServer


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _seed_data_root(root: Path) -> None:
    """Create a small synthetic product workspace, never copying user data."""
    for name in ("kb_documents", "rfp_packages", "outputs", "backups", "pricing"):
        (root / name).mkdir(parents=True, exist_ok=True)

    _write_json(
        root / "company_profile.json",
        {
            "_meta": {"version": "e2e-1.0.0", "description": "Synthetic E2E profile"},
            "company": {"legal_name": "Synthetic E2E Company LLC"},
            "certifications": [],
            "capability_areas": [
                {
                    "name": "Secure cloud application delivery",
                    "description": (
                        "Synthetic E2E-only evidence for designing and implementing "
                        "secure cloud-hosted applications."
                    ),
                },
                {
                    "name": "Project management and quality assurance",
                    "description": (
                        "Synthetic E2E-only evidence for delivery governance and "
                        "quality assurance."
                    ),
                },
            ],
            "deep_specializations": [
                "Synthetic secure case-management platforms",
                "Synthetic delivery governance",
            ],
            "key_personnel": [],
            "labor_rate_card": {},
            "past_performance": [],
        },
    )
    _write_json(
        root / "teaming_partners.json",
        {"_meta": {"version": "e2e-1.0.0"}, "partners": []},
    )
    _write_json(
        root / "decisions.json",
        {"_meta": {"version": "e2e-1.0.0"}, "decisions": []},
    )
    _write_json(
        root / "internal_pricing_rules.json",
        {
            "_meta": {
                "version": "e2e-1.0.0",
                "naics": "541511",
                "rates_effective_date": "2030-01-01",
            },
            "annual_billable_hours": 1950,
            "labor_catalog": [
                {
                    "category": "Project Manager I",
                    "ceiling_hourly_rate_usd": 200.0,
                    "min_experience_years": 1,
                    "education": "Bachelors",
                    "default_wage_band": "150k",
                }
            ],
            "wage_bands": {
                "150k": {
                    "annual_base_wage_usd": 150000,
                    "loaded_annual_cost_high_coverage_usd": 200000,
                    "loaded_annual_cost_low_coverage_usd": 190000,
                    "validated": True,
                }
            },
            "wrap_rate_formula": {
                "components": {
                    "fica_rate": 0.062,
                    "fica_wage_base_2025_usd": 176100,
                    "medicare_rate": 0.0145,
                    "futa_annual_usd": 420,
                    "suta_rate": 0.0382,
                    "bonus_rate_of_wage": 0.10,
                    "employer_401k_match_rate_of_wage": 0.04,
                    "fixed_other_benefits_usd": 5500,
                    "health_high_coverage_usd": 15662.10,
                    "health_low_coverage_usd": 8462.10,
                    "paylocity_overhead_high_usd": 1494,
                    "paylocity_overhead_low_usd": 894,
                    "software_overhead_usd": 1336.20,
                }
            },
            "ga_overhead": {"annual_office_pool_usd": 26700},
            "scenario_definitions": {
                "low": {
                    "coverage_level": "low",
                    "profit_margin_pct": 0.18,
                    "contingency_hours_pct": 0.0,
                },
                "medium": {
                    "coverage_level": "high",
                    "profit_margin_pct": 0.25,
                    "contingency_hours_pct": 0.05,
                },
                "high": {
                    "coverage_level": "high",
                    "profit_margin_pct": 0.30,
                    "contingency_hours_pct": 0.10,
                },
            },
            "profit_policy": {
                "floor_margin_pct": 0.18,
                "target_margin_pct": 0.25,
                "ceiling_margin_pct": 0.30,
            },
        },
    )
    _write_json(
        root / "pricing" / "payment_systems.json",
        {"_meta": {"version": "e2e-1.0.0"}, "our_cost_basis": {}},
    )
    _write_json(
        root / "pricing" / "_payment_systems_context.json",
        {"_meta": {"version": "e2e-1.0.0"}},
    )


@pytest.fixture(scope="session")
def e2e_workspace(
    e2e_enabled: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> E2EWorkspace:
    run_id = os.environ.get("RFP_E2E_RUN_ID", "").strip() or f"direct-{uuid.uuid4().hex[:10]}"
    root = tmp_path_factory.mktemp("rfp-e2e-workspace")
    _seed_data_root(root)

    artifact_base = Path(
        os.environ.get(
            "RFP_E2E_ARTIFACT_ROOT",
            str(Path(tempfile.gettempdir()) / "rfp-e2e-artifacts"),
        )
    ).resolve()
    artifacts = artifact_base / run_id
    artifacts.mkdir(parents=True, exist_ok=True)

    database_path = root / "sqlite.db"
    env = {str(k): str(v) for k, v in os.environ.items()}
    # NiceGUI treats the mere presence of PYTEST_CURRENT_TEST as its own
    # Selenium-screen mode and then requires NICEGUI_SCREEN_TEST_PORT. Our
    # application is intentionally a separate, ordinary server process.
    env.pop("PYTEST_CURRENT_TEST", None)
    env.update(
        {
            "APP_ENV": "e2e",
            "APP_HOST": "127.0.0.1",
            "APP_STORAGE_SECRET": f"e2e-{uuid.uuid4().hex}",
            "RFP_DATA_DIR": str(root),
            "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
            "RFP_E2E_FAKE_LLM": "1",
            "RFP_E2E_LLM_FIXTURES": str(
                PROJECT_ROOT / "tests" / "e2e" / "fixtures" / "workflow_llm.json"
            ),
            "RFP_E2E_LLM_LEDGER": str(artifacts / "llm_calls.jsonl"),
            "PYTHONUNBUFFERED": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    for key in _PROVIDER_KEYS:
        env[key] = ""

    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    migration_log = artifacts / "migration.log"
    migration_log.write_text(
        (migration.stdout or "") + (migration.stderr or ""), encoding="utf-8",
    )
    if migration.returncode != 0:
        pytest.fail(
            f"E2E database migration failed (exit {migration.returncode}); "
            f"see {migration_log}",
            pytrace=False,
        )
    if not database_path.is_file():
        pytest.fail("E2E migration completed without creating sqlite.db", pytrace=False)

    return E2EWorkspace(root, database_path, artifacts, env)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(server log unavailable)"


def _unexpected_server_error_blocks(log_text: str) -> list[str]:
    """Return backend error records, excluding one Windows socket teardown.

    Closing a real Chromium context can make Python's Proactor event loop log
    WinError 10054 while the NiceGUI websocket is being disconnected. It is a
    transport teardown after the page has gone away, not an application error.
    Every other ERROR/CRITICAL/traceback remains a hard E2E failure.
    """
    blocks = re.split(r"(?=^\d{4}-\d{2}-\d{2} .*$)", log_text, flags=re.MULTILINE)
    unexpected: list[str] = []
    for block in blocks:
        if not (
            re.search(r"\b(?:ERROR|CRITICAL)\b", block)
            or "Traceback (most recent call last):" in block
        ):
            continue
        benign_windows_disconnect = (
            "_ProactorBasePipeTransport._call_connection_lost" in block
            and "[WinError 10054]" in block
        )
        if not benign_windows_disconnect:
            unexpected.append(block.strip())
    return unexpected


@pytest.fixture(scope="session")
def e2e_server(e2e_workspace: E2EWorkspace) -> Iterator[E2EServer]:
    port = _free_loopback_port()
    env = dict(e2e_workspace.environment)
    env["APP_PORT"] = str(port)
    base_url = f"http://127.0.0.1:{port}"
    server_log = e2e_workspace.artifacts / "server.log"
    log_handle = server_log.open("w", encoding="utf-8")
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "tests" / "e2e" / "support" / "run_app.py")],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    server = E2EServer(base_url, e2e_workspace, process, server_log)

    deadline = time.monotonic() + 30.0
    startup_error: str | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            startup_error = f"server exited early with code {process.returncode}"
            break
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if response.status == 200 and payload.get("ok") is True:
                break
        except (OSError, ValueError, urllib.error.URLError):
            time.sleep(0.1)
    else:
        startup_error = "health endpoint did not become ready within 30 seconds"

    if startup_error is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        log_handle.close()
        pytest.fail(
            f"E2E application startup failed: {startup_error}\n{_read_log(server_log)}",
            pytrace=False,
        )

    try:
        yield server
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_handle.close()
        backend_errors = _unexpected_server_error_blocks(_read_log(server_log))
        if backend_errors:
            pytest.fail(
                "Unexpected E2E backend errors:\n\n"
                + "\n\n".join(backend_errors)
                + f"\n\nServer log: {server_log}",
                pytrace=False,
            )


@pytest.fixture(scope="session")
def playwright_browser(e2e_enabled: None, e2e_server: E2EServer) -> Iterator[Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail(
            "Playwright is not installed. Run "
            "`.venv\\Scripts\\python -m pip install -e \".[e2e]\"` and "
            "`.venv\\Scripts\\python -m playwright install chromium`.",
            pytrace=False,
        )

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(
                headless=os.environ.get("RFP_E2E_HEADED", "") != "1",
            )
        except Exception as exc:
            pytest.fail(
                "Chromium could not start. Run "
                "`.venv\\Scripts\\python -m playwright install chromium`. "
                f"Original error: {exc}",
                pytrace=False,
            )
        try:
            yield browser
        finally:
            browser.close()


def _artifact_slug(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid).strip("_")[-140:]


def _is_benign_navigation_abort(request_: Any) -> bool:
    """Return whether Chromium canceled a GET superseded by navigation.

    NiceGUI can still be loading a lazily introduced component when a deliberate
    navigation replaces the current document. Chromium reports that cancellation
    as ``net::ERR_ABORTED``; responses with HTTP errors and every other transport
    failure remain actionable browser issues.
    """
    failure_text = str(getattr(request_, "failure", None) or "")
    return (
        str(getattr(request_, "method", "")).upper() == "GET"
        and "net::ERR_ABORTED" in failure_text
    )


def _write_browser_issues(
    artifact_dir: Path,
    issues: list[str],
    *,
    test_failed: bool,
) -> None:
    lines = list(issues)
    if not lines and test_failed:
        lines.append("Test call failed; no browser-side issue was recorded.")
    (artifact_dir / "issues.txt").write_text(
        "\n".join(f"- {issue}" for issue in lines) + "\n",
        encoding="utf-8",
    )


@pytest.fixture()
def browser_session(
    request: pytest.FixtureRequest,
    playwright_browser: Any,
    e2e_server: E2EServer,
) -> Iterator[BrowserSession]:
    artifact_dir = e2e_server.workspace.artifacts / _artifact_slug(request.node.nodeid)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    issues: list[str] = []

    context = playwright_browser.new_context(accept_downloads=True)

    def route_request(route: Any) -> None:
        parsed = urlsplit(route.request.url)
        if parsed.scheme in {"http", "https", "ws", "wss"} and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            issues.append(f"blocked external browser request: {route.request.url}")
            route.abort("blockedbyclient")
            return
        route.continue_()

    context.route("**/*", route_request)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()

    def on_console(message: Any) -> None:
        if message.type == "error":
            issues.append(f"browser console error: {message.text}")

    def on_request_failed(request_: Any) -> None:
        parsed = urlsplit(request_.url)
        failure_text = str(request_.failure or "")
        if _is_benign_navigation_abort(request_):
            return
        if parsed.hostname in {"127.0.0.1", "localhost"}:
            issues.append(
                f"local request failed: {request_.method} {request_.url} "
                f"({failure_text})"
            )

    page.on("console", on_console)
    page.on("pageerror", lambda error: issues.append(f"uncaught page error: {error}"))
    page.on("requestfailed", on_request_failed)
    page.on(
        "response",
        lambda response: issues.append(
            f"HTTP {response.status}: {response.request.method} {response.url}"
        )
        if response.status >= 500
        else None,
    )

    session = BrowserSession(page, issues, artifact_dir, e2e_server)
    try:
        yield session
    finally:
        failed = bool(getattr(request.node, "rep_call", None) and request.node.rep_call.failed)
        if failed or issues:
            try:
                page.screenshot(path=str(artifact_dir / "failure.png"), full_page=True)
                (artifact_dir / "page.html").write_text(page.content(), encoding="utf-8")
            except Exception as exc:
                issues.append(f"failed to capture browser failure artifacts: {exc}")
            _write_browser_issues(artifact_dir, issues, test_failed=failed)
        # Let NiceGUI close its websocket before Chromium is torn down. On
        # Windows this avoids a noisy Proactor WinError 10054 race.
        try:
            page.goto("about:blank", wait_until="commit", timeout=3_000)
            page.wait_for_timeout(100)
        except Exception:
            pass
        context.tracing.stop(path=str(artifact_dir / "trace.zip"))
        context.close()
        if issues and not failed:
            pytest.fail(
                "Unexpected browser errors:\n- " + "\n- ".join(issues) +
                f"\nArtifacts: {artifact_dir}",
                pytrace=False,
            )
