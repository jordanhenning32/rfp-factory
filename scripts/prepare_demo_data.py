"""Build a curated, reproducible demo workspace from the local data.

The source database is opened read-only and is never modified. The output is
a SQLite backup pruned to an explicit proposal allowlist. Knowledge Base rows
and learned reviewer guidance are omitted by default so an external demo does
not expose sensitive filenames, personnel documents, or internal calibration.

Usage from the repository root::

    python scripts/prepare_demo_data.py \
        --keep-proposal 4 --keep-proposal 6 --replace

The demo launcher sets both of these before importing the app::

    RFP_DATA_DIR=E:/RFP Agent/data/demo
    DATABASE_URL=sqlite:///E:/RFP Agent/data/demo/sqlite.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_DATA_DIR = PROJECT_ROOT / "data" / "demo"

_CANONICAL_DATA_FILES = (
    "company_profile.json",
    "teaming_partners.json",
    "decisions.json",
    "internal_pricing_rules.json",
)

_MANAGED_DATA_DIRECTORIES = (
    "pricing",
    "rfp_packages",
    "kb_documents",
    "outputs",
    "backups",
)

_DEMO_KB_SOURCE_MARKER = "[CURATED KB SOURCE]"

_SUMMARY_TABLES = (
    "proposals",
    "rfp_packages",
    "rfp_package_documents",
    "compliance_matrix_items",
    "gap_analyses",
    "proposal_sections",
    "proposal_team_members",
    "pricing_packages",
    "market_scans",
    "reviewer_findings",
    "agent_runs",
    "knowledge_base_documents",
    "knowledge_base_chunks",
    "profile_suggestions",
    "learned_rules",
)


def _readonly_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _count(db: sqlite3.Connection, table: str) -> int:
    if not _table_exists(db, table):
        return 0
    # Table names come only from the fixed _SUMMARY_TABLES tuple.
    return int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(db, table):
        return False
    return any(
        str(row[1]) == column
        for row in db.execute(f'PRAGMA table_info("{table}")')
    )


def _scrub_kb_filename_references(db: sqlite3.Connection) -> int:
    """Replace exact KB filenames in retained proposal-derived fields.

    The filenames are read from the copied database immediately before KB
    metadata is removed. Only literal filename occurrences are replaced,
    case-insensitively; ordinary proposal text is left untouched. Sensitive
    names are intentionally never returned or logged.
    """
    if not _column_exists(db, "knowledge_base_documents", "filename"):
        return 0

    # De-duplicate case-insensitively and replace longer names first so a
    # filename that contains another filename cannot be partially scrubbed.
    filenames_by_casefold: dict[str, str] = {}
    for (raw_filename,) in db.execute(
        "SELECT filename FROM knowledge_base_documents WHERE filename IS NOT NULL"
    ):
        filename = str(raw_filename).strip()
        if filename:
            filenames_by_casefold.setdefault(filename.casefold(), filename)
    filenames = sorted(
        filenames_by_casefold.values(), key=len, reverse=True,
    )
    if not filenames:
        return 0

    scrubbed_count = 0
    for table, column in (
        ("reviewer_findings", "finding_text"),
        ("proposal_sections", "citations_json"),
    ):
        if not _column_exists(db, table, column):
            continue
        for row_id, raw_value in db.execute(
            f'SELECT id, "{column}" FROM "{table}"'
        ).fetchall():
            if not isinstance(raw_value, str) or not raw_value:
                continue
            scrubbed_value = raw_value
            row_replacements = 0
            for filename in filenames:
                scrubbed_value, replacements = re.subn(
                    re.escape(filename),
                    _DEMO_KB_SOURCE_MARKER,
                    scrubbed_value,
                    flags=re.IGNORECASE,
                )
                row_replacements += replacements
            if row_replacements:
                db.execute(
                    f'UPDATE "{table}" SET "{column}"=? WHERE id=?',
                    (scrubbed_value, row_id),
                )
                scrubbed_count += row_replacements
    return scrubbed_count


def build_demo_database(
    source: Path,
    output: Path,
    *,
    keep_proposal_ids: list[int],
    include_kb: bool = False,
    replace: bool = False,
) -> dict:
    """Create and curate ``output`` without modifying ``source``.

    Returns a JSON-serializable summary of the retained dataset. Raises a
    descriptive exception before pruning when the source/allowlist is invalid.
    """
    source = source.resolve()
    output = output.resolve()
    keep_ids = sorted(set(int(pid) for pid in keep_proposal_ids))

    if not keep_ids:
        raise ValueError("at least one --keep-proposal id is required")
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    if source == output:
        raise ValueError("source and output database paths must be different")
    if output.exists() and not replace:
        raise FileExistsError(
            f"output already exists: {output} (pass --replace to rebuild it)"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    source_db = sqlite3.connect(_readonly_uri(source), uri=True)
    demo_db = sqlite3.connect(output)
    try:
        source_db.backup(demo_db)
        demo_db.execute("PRAGMA foreign_keys=ON")
        if demo_db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("could not enable SQLite foreign-key enforcement")

        placeholders = ",".join("?" for _ in keep_ids)
        found = {
            int(row[0])
            for row in demo_db.execute(
                f"SELECT id FROM proposals WHERE id IN ({placeholders})",
                keep_ids,
            )
        }
        missing = sorted(set(keep_ids) - found)
        if missing:
            raise ValueError(f"proposal id(s) not found in source database: {missing}")

        demo_db.execute(
            f"DELETE FROM proposals WHERE id NOT IN ({placeholders})",
            keep_ids,
        )
        # Proposal -> package is RESTRICT, so remove packages only after the
        # non-demo proposals and their cascading children are gone.
        demo_db.execute(
            "DELETE FROM rfp_packages "
            "WHERE id NOT IN (SELECT rfp_package_id FROM proposals)"
        )

        scrubbed_kb_filename_references = 0
        if not include_kb:
            scrubbed_kb_filename_references = _scrub_kb_filename_references(
                demo_db
            )
            # Delete suggestions first for compatibility with pre-0002 copies;
            # current schemas also cascade them from the KB document delete.
            for table in (
                "profile_suggestions",
                "knowledge_base_documents",
                "learned_rules",
            ):
                if _table_exists(demo_db, table):
                    demo_db.execute(f'DELETE FROM "{table}"')

        demo_db.commit()

        fk_errors = list(demo_db.execute("PRAGMA foreign_key_check"))
        if fk_errors:
            raise RuntimeError(f"curated database has foreign-key errors: {fk_errors[:5]}")
        integrity = str(demo_db.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"curated database integrity check failed: {integrity}")

        # Rebuild pages so deleted sensitive text is not retained in free pages.
        demo_db.execute("VACUUM")

        retained = [
            {"id": int(row[0]), "title": row[1], "status": row[2]}
            for row in demo_db.execute(
                "SELECT id, title, status FROM proposals ORDER BY id"
            )
        ]
        counts = {table: _count(demo_db, table) for table in _SUMMARY_TABLES}
        return {
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "output": str(output),
            "source_unchanged": True,
            "knowledge_base_included": include_kb,
            "scrubbed_kb_filename_references": scrubbed_kb_filename_references,
            "retained_proposals": retained,
            "counts": counts,
            "integrity_check": integrity,
            "foreign_key_errors": 0,
            "size_bytes": output.stat().st_size,
        }
    except Exception:
        demo_db.close()
        source_db.close()
        output.unlink(missing_ok=True)
        raise
    finally:
        # close() is idempotent, including after the exception cleanup above.
        demo_db.close()
        source_db.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reset_managed_directory(path: Path, *, root: Path) -> None:
    """Remove one known child directory without accepting broad targets."""
    path = path.resolve()
    root = root.resolve()
    if path.parent != root:
        raise ValueError(f"refusing to reset path outside demo data root: {path}")
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def prepare_demo_bundle(
    source_data_dir: Path,
    output_data_dir: Path,
    *,
    keep_proposal_ids: list[int],
    include_kb: bool = False,
    replace: bool = False,
) -> dict:
    """Build a filesystem-isolated demo data tree.

    In addition to pruning SQLite, this copies the selected RFP package files
    and every canonical JSON input agents may read or edit. Database storage
    paths are rewritten to the demo tree, so delete/amend/upload actions in a
    rehearsal cannot touch canonical proposal files.
    """
    source_data_dir = source_data_dir.resolve()
    output_data_dir = output_data_dir.resolve()
    if source_data_dir == output_data_dir:
        raise ValueError("source and demo data directories must be different")
    if output_data_dir in source_data_dir.parents:
        raise ValueError("demo data directory cannot contain the canonical data directory")
    managed_source_dirs = tuple(
        (source_data_dir / dirname).resolve()
        for dirname in _MANAGED_DATA_DIRECTORIES
    )
    for managed_dir in managed_source_dirs:
        if output_data_dir == managed_dir or managed_dir in output_data_dir.parents:
            raise ValueError(
                "demo output directory cannot be inside a managed canonical "
                f"data subtree: {managed_dir}"
            )
    canonical_files = tuple(
        (source_data_dir / filename).resolve()
        for filename in ("sqlite.db", *_CANONICAL_DATA_FILES)
    )
    if any(
        output_data_dir == canonical_file
        or canonical_file in output_data_dir.parents
        for canonical_file in canonical_files
    ):
        raise ValueError(
            "demo output directory cannot collide with or descend from a "
            "canonical data file"
        )
    if include_kb:
        raise ValueError(
            "Knowledge Base files are intentionally excluded from demo bundles"
        )
    if not (source_data_dir / "sqlite.db").is_file():
        raise FileNotFoundError(
            f"source database not found: {source_data_dir / 'sqlite.db'}"
        )

    output_db = output_data_dir / "sqlite.db"
    manifest_path = output_data_dir / "demo_manifest.json"
    existing_entries = list(output_data_dir.iterdir()) if output_data_dir.is_dir() else []
    if existing_entries and not replace:
        raise FileExistsError(
            f"demo output directory is not empty: {output_data_dir} "
            "(pass --replace to rebuild an owned demo bundle)"
        )
    if existing_entries and replace and output_data_dir != DEFAULT_OUTPUT_DATA_DIR.resolve():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError(
                "refusing to replace a non-empty directory without a valid "
                "demo_manifest.json ownership marker"
            ) from exc
        if (
            existing_manifest.get("complete") is not True
            or Path(existing_manifest.get("output_data_dir", "")).resolve()
            != output_data_dir
        ):
            raise ValueError(
                "refusing to replace a directory not owned by this demo builder"
            )
    output_data_dir.mkdir(parents=True, exist_ok=True)
    # This is the launch publication marker. Remove it before changing the
    # bundle so an interrupted rebuild can never look ready.
    manifest_path.unlink(missing_ok=True)

    source_hash_before = _sha256(source_data_dir / "sqlite.db")
    db_summary = build_demo_database(
        source_data_dir / "sqlite.db",
        output_db,
        keep_proposal_ids=keep_proposal_ids,
        include_kb=include_kb,
        replace=replace,
    )

    db: sqlite3.Connection | None = None
    try:
        for filename in _CANONICAL_DATA_FILES:
            source_file = source_data_dir / filename
            if not source_file.is_file():
                raise FileNotFoundError(f"required demo input missing: {source_file}")
            shutil.copy2(source_file, output_data_dir / filename)

        source_pricing = source_data_dir / "pricing"
        if not source_pricing.is_dir():
            raise FileNotFoundError(f"required pricing directory missing: {source_pricing}")
        demo_pricing = output_data_dir / "pricing"
        _reset_managed_directory(demo_pricing, root=output_data_dir)
        shutil.copytree(source_pricing, demo_pricing, dirs_exist_ok=True)

        demo_rfp_root = output_data_dir / "rfp_packages"
        _reset_managed_directory(demo_rfp_root, root=output_data_dir)
        demo_kb_root = output_data_dir / "kb_documents"
        _reset_managed_directory(demo_kb_root, root=output_data_dir)
        for dirname in ("outputs", "backups"):
            _reset_managed_directory(
                output_data_dir / dirname, root=output_data_dir,
            )

        db = sqlite3.connect(output_db)
        db.execute("PRAGMA foreign_keys=ON")
        packages = list(
            db.execute(
                "SELECT id FROM rfp_packages "
                "WHERE id IN (SELECT rfp_package_id FROM proposals) ORDER BY id"
            )
        )
        copied_files = 0
        for (package_id,) in packages:
            source_package = source_data_dir / "rfp_packages" / str(package_id)
            if not source_package.is_dir():
                raise FileNotFoundError(
                    f"retained proposal package directory missing: {source_package}"
                )
            demo_package = demo_rfp_root / str(package_id)
            shutil.copytree(source_package, demo_package)
            db.execute(
                "UPDATE rfp_packages SET storage_dir=? WHERE id=?",
                (str(demo_package), package_id),
            )
            documents = list(
                db.execute(
                    "SELECT id, storage_path FROM rfp_package_documents "
                    "WHERE rfp_package_id=? ORDER BY id",
                    (package_id,),
                )
            )
            for document_id, old_storage_path in documents:
                demo_document = demo_package / Path(old_storage_path).name
                if not demo_document.is_file():
                    raise FileNotFoundError(
                        f"retained proposal document missing after copy: {demo_document}"
                    )
                db.execute(
                    "UPDATE rfp_package_documents SET storage_path=? WHERE id=?",
                    (str(demo_document), document_id),
                )
                copied_files += 1
        db.commit()
        fk_errors = list(db.execute("PRAGMA foreign_key_check"))
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if fk_errors or integrity.lower() != "ok":
            raise RuntimeError(
                f"demo bundle database validation failed: integrity={integrity}, "
                f"foreign_key_errors={fk_errors[:5]}"
            )

        db.close()
        db = None

        source_hash_after = _sha256(source_data_dir / "sqlite.db")
        if source_hash_after != source_hash_before:
            raise RuntimeError("source database changed while preparing demo bundle")

        db_summary.update(
            {
                "output_data_dir": str(output_data_dir),
                "source_sha256": source_hash_after,
                "copied_rfp_packages": len(packages),
                "copied_rfp_documents": copied_files,
                "isolated_storage_paths": True,
                "size_bytes": output_db.stat().st_size,
                "database_sha256": _sha256(output_db),
                "complete": True,
            }
        )
        manifest_tmp = manifest_path.with_suffix(".json.tmp")
        manifest_tmp.write_text(
            json.dumps(db_summary, indent=2), encoding="utf-8",
        )
        manifest_tmp.replace(manifest_path)
        db_summary["manifest"] = str(manifest_path)
        return db_summary
    except Exception:
        # The canonical source remains untouched. Remove only the output DB so
        # a partial bundle cannot pass the demo launcher's existence check.
        if db is not None:
            db.close()
        manifest_path.unlink(missing_ok=True)
        output_db.unlink(missing_ok=True)
        raise
    finally:
        if db is not None:
            db.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-data-dir", type=Path, default=DEFAULT_SOURCE_DATA_DIR,
    )
    parser.add_argument(
        "--output-data-dir", type=Path, default=DEFAULT_OUTPUT_DATA_DIR,
    )
    parser.add_argument(
        "--keep-proposal",
        type=int,
        action="append",
        dest="keep_proposals",
        required=True,
        help="proposal id to retain; repeat for each demo proposal",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing output database",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        summary = prepare_demo_bundle(
            args.source_data_dir,
            args.output_data_dir,
            keep_proposal_ids=args.keep_proposals,
            include_kb=False,
            replace=args.replace,
        )
    except Exception as exc:
        print(f"Demo data preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    main()
