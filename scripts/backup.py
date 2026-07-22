"""Backup data/ to a target folder.

Usage:
    python scripts/backup.py /path/to/cloud-synced-folder

Creates a timestamped subdir in the target containing the SQLite DB, the
company profile, KB documents, and uploaded RFP packages. Run nightly via
Windows Task Scheduler (or cron).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

FILE_ITEMS_TO_BACKUP = (
    "company_profile.json",
    "teaming_partners.json",
    "decisions.json",
    "internal_pricing_rules.json",
)
DIRECTORY_ITEMS_TO_BACKUP = (
    "pricing",
    "kb_documents",
    "rfp_packages",
    "outputs",
)
ITEMS_TO_BACKUP = [*FILE_ITEMS_TO_BACKUP, *DIRECTORY_ITEMS_TO_BACKUP]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_sqlite(source: Path, destination: Path) -> str:
    """Take a transactionally consistent SQLite online backup."""
    source_db = sqlite3.connect(
        f"file:{source.resolve().as_posix()}?mode=ro", uri=True,
    )
    destination_db = sqlite3.connect(destination)
    try:
        source_db.backup(destination_db)
        integrity = str(
            destination_db.execute("PRAGMA integrity_check").fetchone()[0]
        )
        if integrity.lower() != "ok":
            raise RuntimeError(f"backup database integrity check failed: {integrity}")
        return integrity
    finally:
        destination_db.close()
        source_db.close()


def _validate_backup_sources(
    data_dir: Path,
    target_root: Path,
) -> dict[str, Path]:
    """Validate every required source and return its expected classification."""
    sources = {
        name: (data_dir / name).resolve()
        for name in ("sqlite.db", *ITEMS_TO_BACKUP)
    }

    # Use the declared source classification for containment checks. Checking
    # is_dir() here would miss a recursive target when the source directory is
    # temporarily absent and the target has not been created yet.
    for name in DIRECTORY_ITEMS_TO_BACKUP:
        source_item = sources[name]
        if target_root == source_item or target_root.is_relative_to(source_item):
            raise ValueError(
                f"backup target cannot be inside source directory: {source_item}"
            )

    expected_kinds = {
        "sqlite.db": "file",
        **{name: "file" for name in FILE_ITEMS_TO_BACKUP},
        **{name: "directory" for name in DIRECTORY_ITEMS_TO_BACKUP},
    }
    missing: list[str] = []
    wrong_kind: list[str] = []
    for name, expected_kind in expected_kinds.items():
        source = sources[name]
        if not source.exists():
            missing.append(f"{name} ({expected_kind})")
        elif expected_kind == "file" and not source.is_file():
            wrong_kind.append(f"{name} (expected file)")
        elif expected_kind == "directory" and not source.is_dir():
            wrong_kind.append(f"{name} (expected directory)")

    if missing:
        raise FileNotFoundError(
            "required backup source(s) missing: " + ", ".join(missing)
        )
    if wrong_kind:
        raise ValueError(
            "invalid backup source type(s): " + ", ".join(wrong_kind)
        )
    return sources


def create_backup(
    data_dir: Path,
    target_root: Path,
    *,
    now: datetime | None = None,
) -> Path:
    """Create a complete timestamped backup and return its directory."""
    data_dir = data_dir.resolve()
    target_root = target_root.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data dir not found at {data_dir}")
    sources = _validate_backup_sources(data_dir, target_root)

    target_root.mkdir(parents=True, exist_ok=True)
    created_at = now or datetime.now(UTC)
    stamp = created_at.astimezone().strftime("%Y%m%d_%H%M%S")
    dest = target_root / f"rfp-agent_{stamp}"
    if dest.exists():
        raise FileExistsError(f"backup destination already exists: {dest}")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{dest.name}.incomplete-", dir=target_root)
    )
    try:
        integrity = _backup_sqlite(sources["sqlite.db"], staging / "sqlite.db")
        copied_items = ["sqlite.db"]
        for name in FILE_ITEMS_TO_BACKUP:
            shutil.copy2(sources[name], staging / name)
            copied_items.append(name)
        for name in DIRECTORY_ITEMS_TO_BACKUP:
            shutil.copytree(sources[name], staging / name)
            copied_items.append(name)

        files: dict[str, dict[str, int | str]] = {}
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            relative = path.relative_to(staging).as_posix()
            files[relative] = {
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        manifest = {
            "created_at": created_at.isoformat(),
            "source_data_dir": str(data_dir),
            "database_integrity_check": integrity,
            "copied_items": copied_items,
            "file_count": len(files),
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8",
        )

        # Publish the complete backup in one same-filesystem rename. Until this
        # succeeds, callers cannot mistake the timestamped destination for a
        # usable backup.
        staging.rename(dest)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return dest


def main(target_root: Path) -> int:
    try:
        dest = create_backup(DATA_DIR, target_root)
    except Exception as exc:
        print(f"Backup failed: {exc}", file=sys.stderr)
        return 1

    print(f"Backed up data to {dest} (manifest: {dest / 'manifest.json'})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python scripts/backup.py <target-folder>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(Path(sys.argv[1])))
