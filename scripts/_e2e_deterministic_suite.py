"""Run the no-cost deterministic E2E suite.

This intentionally excludes live LLM smoke tests. Those scripts require
--live and an explicit proposal target.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DETERMINISTIC_SCRIPTS: tuple[tuple[str, list[str]], ...] = (
    ("autoloop orchestration", ["scripts/_e2e_autoloop_test.py"]),
    ("reviewer pipeline", ["scripts/_e2e_pipeline_test.py"]),
    ("service layer", ["scripts/_e2e_service_test.py"]),
    ("shortfall parallelism", ["scripts/_e2e_shortfall_parallel_test.py"]),
    ("UI badges/routes", ["scripts/_e2e_ui_test.py"]),
    ("validator corrections", ["scripts/_e2e_validator_test.py"]),
    ("cost analyst stage 1", ["scripts/_e2e_cost_analyst_test.py", "--stage1-only"]),
)


def _run(label: str, args: list[str], *, env: dict[str, str] | None = None) -> None:
    print()
    print("=" * 72)
    print(label)
    print("=" * 72, flush=True)
    subprocess.run(args, cwd=PROJECT_ROOT, env=env, check=True)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_app_smoke() -> None:
    port = _free_port()
    # Windows can briefly keep the SQLite file locked after the subprocess exits.
    # The temp directory is test-only; do not fail a passing smoke on cleanup lag.
    with tempfile.TemporaryDirectory(prefix="rfp-app-smoke-", ignore_cleanup_errors=True) as temp_dir:
        temp_path = Path(temp_dir)
        db_path = temp_path / "app.sqlite"
        stdout_log = temp_path / "server.out.log"
        stderr_log = temp_path / "server.err.log"

        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{db_path}",
                "APP_HOST": "127.0.0.1",
                "APP_PORT": str(port),
                "APP_ENV": "test",
                "APP_STORAGE_SECRET": "test-secret-for-e2e-smoke",
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
                "GEMINI_API_KEY": "",
                "GROK_API_KEY": "",
            }
        )

        _run("fresh app DB migrations", [sys.executable, "-m", "alembic", "upgrade", "head"], env=env)

        print()
        print("=" * 72)
        print("live app smoke")
        print("=" * 72, flush=True)
        with stdout_log.open("w", encoding="utf-8") as out, stderr_log.open("w", encoding="utf-8") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "app.main"],
                cwd=PROJECT_ROOT,
                env=env,
                stdout=out,
                stderr=err,
            )
        try:
            url = f"http://127.0.0.1:{port}/api/health"
            deadline = time.monotonic() + 30
            last_error: Exception | None = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(url, timeout=2) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    if payload.get("ok") is True:
                        print(
                            "Health OK: "
                            f"ok={payload.get('ok')}, "
                            f"company_profile_version={payload.get('company_profile_version')}"
                        )
                        return
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    time.sleep(0.5)

            print("Server stdout:")
            print(stdout_log.read_text(encoding="utf-8", errors="replace"))
            print("Server stderr:")
            print(stderr_log.read_text(encoding="utf-8", errors="replace"))
            raise RuntimeError(f"app health check failed: {last_error!r}")
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic local E2E checks.")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip the pytest suite.")
    parser.add_argument("--skip-ruff", action="store_true", help="Skip Ruff lint checks.")
    parser.add_argument("--skip-app-smoke", action="store_true", help="Skip live app health smoke.")
    return parser.parse_args(argv[1:])


def main() -> int:
    args = _parse_args(sys.argv)

    if not args.skip_pytest:
        _run("pytest", [sys.executable, "-m", "pytest", "-q"])

    for label, script_args in DETERMINISTIC_SCRIPTS:
        _run(label, [sys.executable, *script_args])

    if not args.skip_app_smoke:
        _run_app_smoke()

    if not args.skip_ruff:
        _run("ruff", [sys.executable, "-m", "ruff", "check", "app", "tests", "scripts"])

    print()
    print("Deterministic E2E suite PASS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
