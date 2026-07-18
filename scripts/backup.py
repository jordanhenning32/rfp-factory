"""Backup data/ to a target folder.

Usage:
    python scripts/backup.py /path/to/cloud-synced-folder

Creates a timestamped subdir in the target containing the SQLite DB, the
company profile, KB documents, and uploaded RFP packages. Run nightly via
Windows Task Scheduler (or cron).
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

ITEMS_TO_BACKUP = [
    "sqlite.db",
    "company_profile.json",
    "kb_documents",
    "rfp_packages",
    "outputs",
]


def main(target_root: Path) -> int:
    if not DATA_DIR.exists():
        print(f"data dir not found at {DATA_DIR}", file=sys.stderr)
        return 1

    target_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = target_root / f"rfp-agent_{stamp}"
    dest.mkdir(parents=True)

    copied = 0
    for name in ITEMS_TO_BACKUP:
        src = DATA_DIR / name
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest / name)
        copied += 1

    print(f"Backed up {copied} item(s) to {dest}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/backup.py <target-folder>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
