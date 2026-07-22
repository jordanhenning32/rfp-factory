r"""Run the real-browser E2E suite until it is clean twice consecutively.

Each attempt is a brand-new pytest process. The E2E fixtures then create a
brand-new temporary data root, migrate a brand-new SQLite database, and start
a brand-new app process. This process boundary is essential because app
settings, data paths, SQLAlchemy, NiceGUI state, and job threads are global.

Usage::

    .venv\Scripts\python scripts\run_e2e_twice.py
    .venv\Scripts\python scripts\run_e2e_twice.py --max-attempts 2 -- -vv
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Extra reporting/debug flags are useful, but selection-altering flags would
# let the runner print the same "gate satisfied" message for only a subset of
# the product. Positional paths/node IDs are rejected separately.
_NARROWING_PYTEST_OPTIONS = {
    "-k",
    "-m",
    "--collect-only",
    "--co",
    "--continue-on-collection-errors",
    "--deselect",
    "--failed-first",
    "--ff",
    "--ignore",
    "--ignore-glob",
    "--last-failed",
    "--lf",
    "--new-first",
    "--nf",
    "--pyargs",
    "--setup-only",
    "--setup-plan",
    "--stepwise",
    "--sw",
    "--stepwise-skip",
}


def _selection_changing_arg(args: list[str]) -> str | None:
    """Return the first argument that could narrow or bypass the full suite."""
    for raw in args:
        if not raw.startswith("-"):
            return raw
        option = raw.split("=", 1)[0]
        if option in _NARROWING_PYTEST_OPTIONS:
            return raw
        # Pytest accepts compact short forms such as ``-kexpr``/``-mfast``.
        if raw.startswith("-k") or raw.startswith("-m"):
            return raw
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--consecutive-clean",
        type=int,
        default=2,
        help="Number of consecutive successful fresh-process runs required (default: 2).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=6,
        help="Fail after this many total attempts without reaching the clean target (default: 6).",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Additional pytest arguments after `--`.",
    )
    args = parser.parse_args()
    if args.consecutive_clean < 1:
        parser.error("--consecutive-clean must be at least 1")
    if args.max_attempts < args.consecutive_clean:
        parser.error("--max-attempts must be >= --consecutive-clean")
    if args.pytest_args[:1] == ["--"]:
        args.pytest_args = args.pytest_args[1:]
    narrowing = _selection_changing_arg(args.pytest_args)
    if narrowing is not None:
        parser.error(
            "the two-clean gate must run all tests/e2e tests; selection or "
            f"collection argument is not allowed: {narrowing}"
        )
    return args


def main() -> int:
    args = _parse_args()
    suite_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    artifact_root = (
        Path(tempfile.gettempdir()) / "rfp-e2e-artifacts" / suite_id
    ).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    clean_streak = 0
    for attempt in range(1, args.max_attempts + 1):
        run_id = f"attempt-{attempt:02d}-{uuid.uuid4().hex[:8]}"
        env = {str(k): str(v) for k, v in os.environ.items()}
        env["RFP_E2E_RUN_ID"] = run_id
        env["RFP_E2E_ARTIFACT_ROOT"] = str(artifact_root)

        command = [
            sys.executable,
            "-m",
            "pytest",
            "tests/e2e",
            "--e2e",
            "-q",
            *args.pytest_args,
        ]
        print(
            f"\n=== E2E attempt {attempt}/{args.max_attempts} "
            f"(clean streak {clean_streak}/{args.consecutive_clean}) ===",
            flush=True,
        )
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            check=False,
        )
        if result.returncode == 0:
            clean_streak += 1
            print(
                f"E2E attempt {attempt} clean; consecutive clean runs: "
                f"{clean_streak}/{args.consecutive_clean}",
                flush=True,
            )
            if clean_streak >= args.consecutive_clean:
                print(
                    f"Two-run gate satisfied. Artifacts: {artifact_root}",
                    flush=True,
                )
                return 0
        else:
            clean_streak = 0
            print(
                f"E2E attempt {attempt} failed with exit code "
                f"{result.returncode}; clean streak reset to 0.",
                flush=True,
            )

    print(
        f"E2E gate not satisfied after {args.max_attempts} attempts. "
        f"Artifacts: {artifact_root}",
        file=sys.stderr,
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
