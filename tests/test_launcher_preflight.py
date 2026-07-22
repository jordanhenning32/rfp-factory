"""Focused safety tests for the Windows launcher and read-only preflight."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from scripts import launcher_preflight


def _create_storage_database(
    database: Path,
    *,
    package_dir: Path,
    document_path: Path,
) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """
            CREATE TABLE rfp_packages (
                id INTEGER PRIMARY KEY,
                storage_dir TEXT NOT NULL
            );
            CREATE TABLE rfp_package_documents (
                id INTEGER PRIMARY KEY,
                storage_path TEXT NOT NULL
            );
            CREATE TABLE knowledge_base_documents (
                id INTEGER PRIMARY KEY,
                storage_path TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO rfp_packages (id, storage_dir) VALUES (4, ?)",
            (str(package_dir),),
        )
        connection.execute(
            "INSERT INTO rfp_package_documents (id, storage_path) VALUES (8, ?)",
            (str(document_path),),
        )
        connection.commit()
    finally:
        connection.close()


def _database_settings(database: Path, *, app_env: str = "demo") -> SimpleNamespace:
    return SimpleNamespace(database_url=f"sqlite:///{database.as_posix()}", app_env=app_env)


def test_demo_storage_references_inside_active_data_dir_pass(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "demo"
    package_dir = data_dir / "rfp_packages" / "4"
    package_dir.mkdir(parents=True)
    document = package_dir / "source.docx"
    document.write_bytes(b"demo")
    database = data_dir / "sqlite.db"
    _create_storage_database(database, package_dir=package_dir, document_path=document)
    monkeypatch.setenv("RFP_DATA_DIR", str(data_dir))

    results = launcher_preflight.check_database_file_references(
        _database_settings(database),
        project_root=tmp_path,
        data_dir=data_dir,
    )

    assert not any(result.failed for result in results)
    assert any(result.name == "Stored files" and result.level == "PASS" for result in results)


def test_demo_storage_references_to_existing_canonical_files_fail(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "demo"
    data_dir.mkdir()
    canonical_package = tmp_path / "canonical" / "rfp_packages" / "4"
    canonical_package.mkdir(parents=True)
    canonical_document = canonical_package / "source.docx"
    canonical_document.write_bytes(b"real but not isolated")
    database = data_dir / "sqlite.db"
    _create_storage_database(
        database,
        package_dir=canonical_package,
        document_path=canonical_document,
    )
    monkeypatch.setenv("RFP_DATA_DIR", str(data_dir))

    results = launcher_preflight.check_database_file_references(
        _database_settings(database),
        project_root=tmp_path,
        data_dir=data_dir,
    )
    details = "\n".join(result.detail for result in results)

    assert any(result.failed for result in results)
    assert "escape the active data root" in details
    assert "4" in details
    assert "8" in details


def test_demo_storage_reference_to_missing_file_fails(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "demo"
    package_dir = data_dir / "rfp_packages" / "4"
    package_dir.mkdir(parents=True)
    missing_document = package_dir / "missing.docx"
    database = data_dir / "sqlite.db"
    _create_storage_database(
        database,
        package_dir=package_dir,
        document_path=missing_document,
    )
    monkeypatch.setenv("RFP_DATA_DIR", str(data_dir))

    results = launcher_preflight.check_database_file_references(
        _database_settings(database),
        project_root=tmp_path,
        data_dir=data_dir,
    )

    assert any(result.failed for result in results)
    assert any("Missing filesystem targets" in result.detail for result in results)


def test_active_data_inputs_require_valid_reference_json(tmp_path: Path) -> None:
    for directory in ("backups", "kb_documents", "outputs", "pricing", "rfp_packages"):
        (tmp_path / directory).mkdir()
    required = (
        "company_profile.json",
        "internal_pricing_rules.json",
        "teaming_partners.json",
        "decisions.json",
        "pricing/payment_systems.json",
        "pricing/_payment_systems_context.json",
    )
    for relative in required:
        (tmp_path / relative).write_text(json.dumps({"ready": True}), encoding="utf-8")

    passing = launcher_preflight.check_data_inputs(tmp_path)
    assert not any(result.failed for result in passing)

    (tmp_path / "internal_pricing_rules.json").write_text("not-json", encoding="utf-8")
    failing = launcher_preflight.check_data_inputs(tmp_path)
    assert any(result.failed and "internal_pricing_rules.json" in result.detail for result in failing)


def test_demo_manifest_allows_hash_drift_after_valid_publication(tmp_path: Path) -> None:
    database = tmp_path / "sqlite.db"
    database.write_bytes(b"curated demo database")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    manifest = tmp_path / "demo_manifest.json"
    manifest.write_text(
        json.dumps({"complete": True, "database_sha256": digest}),
        encoding="utf-8",
    )

    passing = launcher_preflight.check_demo_manifest(tmp_path, database)
    assert not any(result.failed for result in passing)

    database.write_bytes(b"changed after curation")
    changed = launcher_preflight.check_demo_manifest(tmp_path, database)
    assert not any(result.failed for result in changed)
    assert any(result.level == "WARN" and "changed since" in result.detail for result in changed)


def test_demo_manifest_still_requires_valid_complete_publication(tmp_path: Path) -> None:
    database = tmp_path / "sqlite.db"
    database.write_bytes(b"curated demo database")
    manifest = tmp_path / "demo_manifest.json"

    manifest.write_text(
        json.dumps({"complete": False, "database_sha256": "0" * 64}),
        encoding="utf-8",
    )
    incomplete = launcher_preflight.check_demo_manifest(tmp_path, database)
    assert any(result.failed and "complete=true" in result.detail for result in incomplete)

    manifest.write_text(
        json.dumps({"complete": True, "database_sha256": "not-a-hash"}),
        encoding="utf-8",
    )
    invalid_hash = launcher_preflight.check_demo_manifest(tmp_path, database)
    assert any(result.failed and "missing or invalid" in result.detail for result in invalid_hash)


def test_missing_database_check_is_non_mutating(tmp_path: Path) -> None:
    database = tmp_path / "does-not-exist.sqlite"
    script, heads = launcher_preflight.load_migration_script()

    results = launcher_preflight.check_database(
        _database_settings(database, app_env="development"),
        script,
        heads,
        require_current=False,
        project_root=tmp_path,
    )

    assert not database.exists()
    assert any(result.level == "WARN" for result in results)


def test_launcher_orders_gates_before_browser_and_supports_verify_only() -> None:
    project_root = Path(__file__).resolve().parent.parent
    launcher = (project_root / "scripts" / "run_app.bat").read_text(encoding="utf-8").lower()
    before = launcher.index("--phase before-migrations")
    migration = launcher.index("-m alembic upgrade head")
    after = launcher.index("--phase after-migrations")
    browser = launcher.index('start /b "" powershell')

    assert 'set "app_host=127.0.0.1"' in launcher
    assert before < migration < after < browser
    assert "--preflight-only" in launcher
    assert "--phase verify" in launcher


def test_demo_wrapper_selects_isolated_tree_and_demo_environment() -> None:
    project_root = Path(__file__).resolve().parent.parent
    wrapper = (project_root / "scripts" / "run_demo.bat").read_text(encoding="utf-8").lower()

    assert "data\\demo" in wrapper
    assert "rfp_data_dir" in wrapper
    assert "database_url=sqlite:///" in wrapper
    assert 'set "app_env=demo"' in wrapper
    assert "if not exist" in wrapper
    assert "demo_manifest.json" in wrapper


def test_browser_helper_waits_for_health_before_opening() -> None:
    project_root = Path(__file__).resolve().parent.parent
    helper = (project_root / "scripts" / "_open_app_window.ps1").read_text(encoding="utf-8").lower()

    assert "/api/health" in helper
    assert "timeoutseconds" in helper
    assert helper.index("invoke-webrequest") < helper.index("start-process -filepath")
