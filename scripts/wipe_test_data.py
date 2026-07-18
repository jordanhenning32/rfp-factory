"""Wipe all proposals + RFP packages + on-disk files. Use during development.

Does NOT touch the knowledge base or company profile.

Usage:
    python scripts/wipe_test_data.py              # asks for confirmation
    python scripts/wipe_test_data.py --yes        # skips confirmation
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `app` importable when run from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import session_scope  # noqa: E402
from app.services.proposals import wipe_all_test_data  # noqa: E402


def main() -> int:
    auto = "--yes" in sys.argv or "-y" in sys.argv
    if not auto:
        ans = input(
            "Wipe ALL proposals, RFP packages, compliance items, agent runs, and on-disk RFP files? (type 'yes'): "
        )
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 1
    with session_scope() as db:
        counts = wipe_all_test_data(db)
    print(f"Wiped: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
