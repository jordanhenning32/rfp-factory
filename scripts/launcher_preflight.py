"""Read-only startup checks for the local RFP Factory launcher.

The batch launcher runs this twice: once before Alembic is allowed to
change the database, and once afterwards to prove the database reached the
repository head. This module never prints credential values and never
creates or migrates a database itself.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import socket
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_CORE_IMPORTS = {
    "FastAPI": "fastapi",
    "NiceGUI": "nicegui",
    "Uvicorn": "uvicorn",
    "SQLAlchemy": "sqlalchemy",
    "Alembic": "alembic",
    "Pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "python-multipart": "multipart",
    "python-dotenv": "dotenv",
    "Redis": "redis",
    "RQ": "rq",
    "HTTPX": "httpx",
    "aiofiles": "aiofiles",
    "pdfplumber": "pdfplumber",
    "pypdf": "pypdf",
    "python-docx": "docx",
    "openpyxl": "openpyxl",
    "json-repair": "json_repair",
}

_PROVIDERS = {
    "anthropic": {
        "label": "Anthropic",
        "prefixes": ("claude-",),
        "module": "anthropic",
        "key_label": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "prefixes": ("gpt-", "o1-", "o3-", "o4-"),
        "module": "openai",
        "key_label": "OPENAI_API_KEY",
    },
    "google": {
        "label": "Google Gemini",
        "prefixes": ("gemini-",),
        "module": "google.genai",
        "key_label": "GEMINI_API_KEY or GOOGLE_API_KEY",
    },
}

_KNOWN_WEAK_STORAGE_SECRETS = {
    "change-me-to-a-random-string",
    "dev-only-change-me",
    "dev-only-please-change-me",
}


@dataclass(frozen=True)
class CheckResult:
    level: Literal["PASS", "WARN", "FAIL"]
    name: str
    detail: str

    @property
    def failed(self) -> bool:
        return self.level == "FAIL"


def _same_path(left: Path, right: Path) -> bool:
    """Compare Windows paths case-insensitively without requiring existence."""
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def check_project_layout(project_root: Path = PROJECT_ROOT) -> list[CheckResult]:
    required = (
        project_root / ".env",
        project_root / "alembic.ini",
        project_root / "alembic" / "versions",
        project_root / "app" / "main.py",
        project_root / "assets" / "brand" / "qd-logo-header.png",
        project_root / "assets" / "brand" / "favicon.ico",
        project_root / "assets" / "rfp_factory.ico",
    )
    missing = [path.relative_to(project_root).as_posix() for path in required if not path.exists()]
    if missing:
        return [CheckResult("FAIL", "Project files", f"Missing: {', '.join(missing)}")]
    return [CheckResult("PASS", "Project files", "Application, environment, and migration files found.")]


def check_python_runtime(
    project_root: Path = PROJECT_ROOT,
    *,
    executable: Path | None = None,
    prefix: Path | None = None,
) -> list[CheckResult]:
    executable = (executable or Path(sys.executable)).resolve()
    prefix = (prefix or Path(sys.prefix)).resolve()
    expected_venv = (project_root / ".venv").resolve()
    results: list[CheckResult] = []

    # This launcher must explain an unsupported interpreter before startup.
    if sys.version_info < (3, 11):  # noqa: UP036
        results.append(CheckResult("FAIL", "Python", "Python 3.11 or newer is required."))
    else:
        results.append(
            CheckResult(
                "PASS",
                "Python",
                f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} is supported.",
            )
        )

    if not _same_path(prefix, expected_venv) or expected_venv not in executable.parents:
        results.append(
            CheckResult(
                "FAIL",
                "Virtual environment",
                "Launcher must use this repository's .venv interpreter.",
            )
        )
    else:
        results.append(
            CheckResult("PASS", "Virtual environment", "Repository .venv interpreter is active.")
        )
    return results


def check_core_dependencies() -> list[CheckResult]:
    missing: list[str] = []
    broken: list[str] = []
    for label, module_name in _CORE_IMPORTS.items():
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError:
            missing.append(label)
        except Exception:
            # Import-time ABI/version failures matter just as much as absence,
            # but exception text can contain machine-specific or sensitive data.
            broken.append(label)

    results: list[CheckResult] = []
    if missing:
        results.append(CheckResult("FAIL", "Dependencies", f"Not installed: {', '.join(missing)}"))
    if broken:
        results.append(CheckResult("FAIL", "Dependencies", f"Failed to import: {', '.join(broken)}"))
    if not missing and not broken:
        results.append(
            CheckResult("PASS", "Dependencies", f"All {len(_CORE_IMPORTS)} core runtime imports succeeded.")
        )
    return results


def _load_settings() -> Any:
    from app.config import get_settings

    get_settings.cache_clear()
    return get_settings()


def _database_target(settings: Any, project_root: Path) -> tuple[Any, Path | None]:
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite":
        return url, None
    if not url.database or url.database == ":memory:":
        return url, None
    database_path = Path(url.database)
    if not database_path.is_absolute():
        database_path = project_root / database_path
    return url, database_path.resolve()


def check_configuration(settings: Any, project_root: Path = PROJECT_ROOT) -> list[CheckResult]:
    results: list[CheckResult] = []

    if settings.app_host != "127.0.0.1":
        results.append(
            CheckResult(
                "FAIL",
                "Network binding",
                "Local launcher requires APP_HOST=127.0.0.1.",
            )
        )
    else:
        results.append(CheckResult("PASS", "Network binding", "Server is restricted to 127.0.0.1."))

    if not isinstance(settings.app_port, int) or not 1 <= settings.app_port <= 65535:
        results.append(CheckResult("FAIL", "Application port", "APP_PORT must be between 1 and 65535."))
    else:
        results.append(CheckResult("PASS", "Application port", f"Port {settings.app_port} is valid."))

    storage_secret = str(settings.app_storage_secret or "").strip()
    if not storage_secret:
        results.append(CheckResult("FAIL", "Storage secret", "APP_STORAGE_SECRET is not configured."))
    elif storage_secret.lower() in _KNOWN_WEAK_STORAGE_SECRETS or "change-me" in storage_secret.lower():
        results.append(
            CheckResult(
                "WARN",
                "Storage secret",
                "Configured but uses a known development placeholder (value hidden).",
            )
        )
    else:
        results.append(CheckResult("PASS", "Storage secret", "Configured (value hidden)."))

    try:
        url, database_path = _database_target(settings, project_root)
    except Exception as exc:
        results.append(
            CheckResult("FAIL", "Database configuration", f"DATABASE_URL is invalid ({type(exc).__name__}).")
        )
        return results

    backend = url.get_backend_name()
    if backend == "sqlite" and database_path is None:
        results.append(
            CheckResult("FAIL", "Database configuration", "Launcher requires a file-backed SQLite database.")
        )
    elif database_path is not None:
        parent = database_path.parent
        if not parent.is_dir():
            results.append(
                CheckResult("FAIL", "Database configuration", f"Database directory does not exist: {parent}")
            )
        elif not os.access(parent, os.W_OK):
            results.append(
                CheckResult("FAIL", "Database configuration", f"Database directory is not writable: {parent}")
            )
        else:
            results.append(
                CheckResult("PASS", "Database configuration", f"SQLite target: {database_path}")
            )
    else:
        results.append(
            CheckResult("PASS", "Database configuration", f"Configured backend: {backend} (URL hidden).")
        )

    # The demo wrapper uses this guard to ensure proposal attachments, KB
    # files, outputs, and the SQLite DB all stay in one disposable tree.
    requested_data_dir = os.environ.get("RFP_DATA_DIR", "").strip()
    if requested_data_dir:
        from app.config import DATA_DIR

        requested = Path(requested_data_dir).resolve()
        configured = Path(DATA_DIR).resolve()
        if not _same_path(requested, configured):
            results.append(
                CheckResult(
                    "FAIL",
                    "Data isolation",
                    "RFP_DATA_DIR was set but the application did not adopt it.",
                )
            )
        elif database_path is None or not database_path.is_relative_to(configured):
            results.append(
                CheckResult(
                    "FAIL",
                    "Data isolation",
                    "Demo database must be inside the configured RFP_DATA_DIR.",
                )
            )
        else:
            results.append(
                CheckResult("PASS", "Data isolation", f"Demo files are isolated under {configured}.")
            )
            results.extend(check_demo_manifest(configured, database_path))

    return results


def check_data_inputs(data_dir: Path | None = None) -> list[CheckResult]:
    """Validate the reference files the app reads from the active data root."""
    if data_dir is None:
        from app.config import DATA_DIR

        data_dir = DATA_DIR
    data_dir = Path(data_dir).resolve()

    required_directories = (
        data_dir / "backups",
        data_dir / "kb_documents",
        data_dir / "outputs",
        data_dir / "pricing",
        data_dir / "rfp_packages",
    )
    missing_directories = [path.name for path in required_directories if not path.is_dir()]

    required_json = (
        data_dir / "company_profile.json",
        data_dir / "internal_pricing_rules.json",
        data_dir / "teaming_partners.json",
        data_dir / "decisions.json",
        data_dir / "pricing" / "payment_systems.json",
        data_dir / "pricing" / "_payment_systems_context.json",
    )
    missing_files: list[str] = []
    invalid_files: list[str] = []
    for path in required_json:
        label = path.relative_to(data_dir).as_posix()
        if not path.is_file():
            missing_files.append(label)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            invalid_files.append(label)
            continue
        if not isinstance(payload, dict) or not payload:
            invalid_files.append(label)

    results: list[CheckResult] = []
    if missing_directories:
        results.append(
            CheckResult("FAIL", "Data directories", f"Missing: {', '.join(missing_directories)}")
        )
    else:
        results.append(CheckResult("PASS", "Data directories", f"Active data root is complete: {data_dir}"))
    if missing_files:
        results.append(CheckResult("FAIL", "Reference data", f"Missing: {', '.join(missing_files)}"))
    if invalid_files:
        results.append(
            CheckResult("FAIL", "Reference data", f"Invalid or empty JSON: {', '.join(invalid_files)}")
        )
    if not missing_files and not invalid_files:
        results.append(
            CheckResult("PASS", "Reference data", f"All {len(required_json)} required JSON inputs are valid.")
        )
    return results


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_demo_manifest(data_dir: Path, database_path: Path) -> list[CheckResult]:
    """Prove the isolated demo bundle was completely and atomically prepared."""
    data_dir = Path(data_dir).resolve()
    database_path = Path(database_path).resolve()
    manifest_path = data_dir / "demo_manifest.json"
    if not manifest_path.is_file():
        return [CheckResult("FAIL", "Demo manifest", "demo_manifest.json is missing.")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [CheckResult("FAIL", "Demo manifest", "demo_manifest.json is invalid JSON.")]
    if not isinstance(manifest, dict) or manifest.get("complete") is not True:
        return [CheckResult("FAIL", "Demo manifest", "Manifest is not marked complete=true.")]

    expected_hash = str(manifest.get("database_sha256") or "").strip().lower()
    if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
        return [CheckResult("FAIL", "Demo manifest", "Manifest database_sha256 is missing or invalid.")]
    if not database_path.is_file():
        return [CheckResult("FAIL", "Demo manifest", "Manifest database file is missing.")]
    if _sha256(database_path) != expected_hash:
        return [
            CheckResult(
                "WARN",
                "Demo manifest",
                "Database has changed since the curated bundle was published; "
                "continuing with live integrity, migration, and storage-path checks.",
            )
        ]
    return [CheckResult("PASS", "Demo manifest", "Completion marker and database SHA-256 are valid.")]


def _provider_for_model(model: str) -> str | None:
    for provider, metadata in _PROVIDERS.items():
        if model.startswith(metadata["prefixes"]):
            return provider
    return None


def configured_providers(settings: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Return provider -> setting names plus unsupported model settings."""
    providers: dict[str, list[str]] = {}
    unsupported: list[str] = []
    model_fields = getattr(type(settings), "model_fields", {})
    for field_name in model_fields:
        if not field_name.startswith("model_"):
            continue
        model = str(getattr(settings, field_name, "") or "").strip()
        provider = _provider_for_model(model)
        if provider is None:
            unsupported.append(field_name)
            continue
        providers.setdefault(provider, []).append(field_name)
    return providers, unsupported


def check_provider_configuration(settings: Any) -> list[CheckResult]:
    providers, unsupported = configured_providers(settings)
    results: list[CheckResult] = []
    if unsupported:
        results.append(
            CheckResult(
                "FAIL",
                "Model routing",
                f"Unsupported or empty model configuration: {', '.join(sorted(unsupported))}",
            )
        )
    else:
        summary = ", ".join(
            f"{_PROVIDERS[name]['label']} ({len(fields)})" for name, fields in sorted(providers.items())
        )
        results.append(CheckResult("PASS", "Model routing", f"Configured routes: {summary}."))

    missing_sdks: list[str] = []
    for provider in providers:
        metadata = _PROVIDERS[provider]
        try:
            importlib.import_module(metadata["module"])
        except Exception:
            missing_sdks.append(metadata["label"])
    if missing_sdks:
        results.append(
            CheckResult("FAIL", "Provider SDKs", f"Missing or broken: {', '.join(missing_sdks)}")
        )
    else:
        results.append(CheckResult("PASS", "Provider SDKs", "All configured provider SDKs imported."))

    key_values = {
        "anthropic": str(getattr(settings, "anthropic_api_key", "") or "").strip(),
        "openai": str(getattr(settings, "openai_api_key", "") or "").strip(),
        "google": str(getattr(settings, "google_or_gemini_key", "") or "").strip(),
    }
    missing_keys = [_PROVIDERS[name]["key_label"] for name in providers if not key_values[name]]
    if missing_keys:
        results.append(
            CheckResult(
                "FAIL",
                "Provider credentials",
                f"Not configured: {', '.join(missing_keys)}. Credential values are never displayed.",
            )
        )
    else:
        results.append(
            CheckResult(
                "PASS",
                "Provider credentials",
                "Credentials for every configured model provider are present (values hidden).",
            )
        )
    return results


def load_migration_script(project_root: Path = PROJECT_ROOT) -> tuple[Any, tuple[str, ...]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    script = ScriptDirectory.from_config(config)
    # Walking the graph catches missing parents and duplicate revision IDs.
    tuple(script.walk_revisions())
    return script, tuple(script.get_heads())


def check_migration_scripts(project_root: Path = PROJECT_ROOT) -> tuple[list[CheckResult], Any | None, tuple[str, ...]]:
    try:
        script, heads = load_migration_script(project_root)
    except Exception as exc:
        return (
            [CheckResult("FAIL", "Migration files", f"Alembic graph is invalid ({type(exc).__name__}).")],
            None,
            (),
        )
    if not heads:
        return [CheckResult("FAIL", "Migration files", "Alembic has no head revision.")], script, heads
    return (
        [CheckResult("PASS", "Migration files", f"Alembic graph is valid; head: {', '.join(heads)}.")],
        script,
        heads,
    )


def _sqlite_database_state(database_path: Path) -> tuple[tuple[str, ...], list[str]]:
    # mode=ro and query_only make the verification guarantee explicit: this
    # connection cannot create, migrate, or otherwise alter the demo DB.
    uri = f"file:{database_path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            return (), ["SQLite quick_check failed."]
        foreign_key_issues = list(connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_issues:
            return (), [f"SQLite reports {len(foreign_key_issues)} foreign-key violation(s)."]
        try:
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        except sqlite3.OperationalError:
            rows = []
        return tuple(sorted(str(row[0]) for row in rows)), []
    finally:
        connection.close()


def _other_database_state(url: Any) -> tuple[tuple[str, ...], list[str]]:
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            heads = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
        return heads, []
    finally:
        engine.dispose()


def check_database(
    settings: Any,
    script: Any,
    expected_heads: tuple[str, ...],
    *,
    require_current: bool,
    project_root: Path = PROJECT_ROOT,
) -> list[CheckResult]:
    try:
        url, database_path = _database_target(settings, project_root)
    except Exception as exc:
        return [CheckResult("FAIL", "Database", f"Could not parse configuration ({type(exc).__name__}).")]

    if url.get_backend_name() == "sqlite":
        if database_path is None:
            return [CheckResult("FAIL", "Database", "A file-backed SQLite target is required.")]
        if not database_path.is_file():
            if require_current:
                return [CheckResult("FAIL", "Database", f"Database does not exist: {database_path}")]
            return [
                CheckResult(
                    "WARN",
                    "Database",
                    "Database does not exist yet; Alembic will create it.",
                )
            ]
        try:
            current_heads, integrity_errors = _sqlite_database_state(database_path)
        except Exception as exc:
            return [CheckResult("FAIL", "Database", f"Read-only connection failed ({type(exc).__name__}).")]
    else:
        try:
            current_heads, integrity_errors = _other_database_state(url)
        except Exception as exc:
            return [
                CheckResult(
                    "FAIL",
                    "Database",
                    f"Connection to configured backend failed ({type(exc).__name__}; URL hidden).",
                )
            ]

    if integrity_errors:
        return [CheckResult("FAIL", "Database integrity", detail) for detail in integrity_errors]

    results = [CheckResult("PASS", "Database integrity", "Read-only database checks passed.")]
    unknown_heads: list[str] = []
    for revision in current_heads:
        try:
            script.get_revision(revision)
        except Exception:
            unknown_heads.append(revision)
    if unknown_heads:
        results.append(
            CheckResult(
                "FAIL",
                "Database revision",
                f"Database contains revision(s) not present in this checkout: {', '.join(unknown_heads)}.",
            )
        )
    elif set(current_heads) == set(expected_heads):
        results.append(
            CheckResult("PASS", "Database revision", f"Current at Alembic head: {', '.join(expected_heads)}.")
        )
    elif require_current:
        current = ", ".join(current_heads) if current_heads else "unversioned"
        results.append(
            CheckResult(
                "FAIL",
                "Database revision",
                f"Expected {', '.join(expected_heads)}; found {current} after migration.",
            )
        )
    else:
        current = ", ".join(current_heads) if current_heads else "unversioned"
        results.append(
            CheckResult(
                "WARN",
                "Database revision",
                f"Migration required ({current} -> {', '.join(expected_heads)}).",
            )
        )
    return results


def _stored_path(raw_path: str, project_root: Path) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def check_database_file_references(
    settings: Any,
    *,
    project_root: Path = PROJECT_ROOT,
    data_dir: Path | None = None,
) -> list[CheckResult]:
    """Ensure DB file pointers exist and remain inside the active data root."""
    try:
        url, database_path = _database_target(settings, project_root)
    except Exception as exc:
        return [
            CheckResult("FAIL", "Stored files", f"Could not parse database target ({type(exc).__name__}).")
        ]
    if url.get_backend_name() != "sqlite" or database_path is None or not database_path.is_file():
        return []

    if data_dir is None:
        from app.config import DATA_DIR

        data_dir = DATA_DIR
    data_dir = Path(data_dir).resolve()
    package_root = (data_dir / "rfp_packages").resolve()
    kb_root = (data_dir / "kb_documents").resolve()
    enforce_containment = bool(os.environ.get("RFP_DATA_DIR", "").strip())

    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        try:
            package_rows = connection.execute(
                "SELECT id, storage_dir FROM rfp_packages ORDER BY id"
            ).fetchall()
            document_rows = connection.execute(
                "SELECT id, storage_path FROM rfp_package_documents ORDER BY id"
            ).fetchall()
            kb_rows = connection.execute(
                "SELECT id, storage_path FROM knowledge_base_documents ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return [
                CheckResult(
                    "FAIL",
                    "Stored files",
                    f"Required storage tables are unavailable ({type(exc).__name__}).",
                )
            ]
    finally:
        connection.close()

    missing_package_dirs: list[int] = []
    outside_package_dirs: list[int] = []
    for row_id, raw_path in package_rows:
        resolved = _stored_path(str(raw_path or ""), project_root)
        if enforce_containment and not resolved.is_relative_to(package_root):
            outside_package_dirs.append(int(row_id))
        if not resolved.is_dir():
            missing_package_dirs.append(int(row_id))

    missing_documents: list[int] = []
    outside_documents: list[int] = []
    for row_id, raw_path in document_rows:
        resolved = _stored_path(str(raw_path or ""), project_root)
        if enforce_containment and not resolved.is_relative_to(package_root):
            outside_documents.append(int(row_id))
        if not resolved.is_file():
            missing_documents.append(int(row_id))

    missing_kb: list[int] = []
    outside_kb: list[int] = []
    for row_id, raw_path in kb_rows:
        resolved = _stored_path(str(raw_path or ""), project_root)
        if enforce_containment and not resolved.is_relative_to(kb_root):
            outside_kb.append(int(row_id))
        if not resolved.is_file():
            missing_kb.append(int(row_id))

    results: list[CheckResult] = []

    def add_failures(label: str, missing: list[int], outside: list[int]) -> None:
        if missing:
            results.append(
                CheckResult("FAIL", label, f"Missing filesystem targets for row ID(s): {', '.join(map(str, missing))}.")
            )
        if outside:
            results.append(
                CheckResult("FAIL", label, f"Paths escape the active data root for row ID(s): {', '.join(map(str, outside))}.")
            )

    add_failures("RFP package directories", missing_package_dirs, outside_package_dirs)
    add_failures("RFP package documents", missing_documents, outside_documents)
    add_failures("Knowledge-base documents", missing_kb, outside_kb)

    if not results:
        results.append(
            CheckResult(
                "PASS",
                "Stored files",
                (
                    f"Verified {len(package_rows)} package storage location(s), "
                    f"{len(document_rows)} RFP document(s), and {len(kb_rows)} KB document(s)."
                ),
            )
        )
    if str(getattr(settings, "app_env", "")).lower() == "demo" and not document_rows:
        results.append(
            CheckResult("FAIL", "Demo documents", "Curated demo database contains no RFP package documents.")
        )
    return results


def check_port_available(settings: Any) -> list[CheckResult]:
    if settings.app_host != "127.0.0.1" or not isinstance(settings.app_port, int):
        return []
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((settings.app_host, settings.app_port))
    except OSError:
        return [
            CheckResult(
                "FAIL",
                "Application port",
                f"127.0.0.1:{settings.app_port} is already in use or unavailable.",
            )
        ]
    finally:
        probe.close()
    return [CheckResult("PASS", "Application port", f"127.0.0.1:{settings.app_port} is available.")]


def run_preflight(
    *,
    phase: Literal["before-migrations", "after-migrations", "verify"] = "verify",
    project_root: Path = PROJECT_ROOT,
) -> list[CheckResult]:
    results: list[CheckResult] = []
    results.extend(check_project_layout(project_root))
    results.extend(check_python_runtime(project_root))
    dependency_results = check_core_dependencies()
    results.extend(dependency_results)
    if any(result.failed for result in dependency_results):
        results.append(
            CheckResult("FAIL", "Preflight", "Configuration and database checks skipped until dependencies are fixed.")
        )
        return results

    try:
        settings = _load_settings()
    except Exception as exc:
        results.append(
            CheckResult("FAIL", "Configuration", f"Settings could not be loaded ({type(exc).__name__}).")
        )
        return results

    results.extend(check_configuration(settings, project_root))
    results.extend(check_data_inputs())
    results.extend(check_provider_configuration(settings))
    migration_results, script, expected_heads = check_migration_scripts(project_root)
    results.extend(migration_results)
    if script is not None and expected_heads:
        results.extend(
            check_database(
                settings,
                script,
                expected_heads,
                require_current=phase != "before-migrations",
                project_root=project_root,
            )
        )
        results.extend(check_database_file_references(settings, project_root=project_root))
    results.extend(check_port_available(settings))
    return results


def _print_report(results: list[CheckResult], phase: str) -> None:
    print(f"RFP Factory startup preflight ({phase})")
    for result in results:
        print(f"[{result.level}] {result.name}: {result.detail}")
    failures = sum(result.failed for result in results)
    warnings = sum(result.level == "WARN" for result in results)
    if failures:
        print(f"Preflight failed: {failures} error(s), {warnings} warning(s).")
    else:
        print(f"Preflight passed: {warnings} warning(s).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run read-only launcher readiness checks.")
    parser.add_argument(
        "--phase",
        choices=("before-migrations", "after-migrations", "verify"),
        default="verify",
        help="Before allows an outdated/missing DB; after/verify require Alembic head.",
    )
    args = parser.parse_args(argv)
    results = run_preflight(phase=args.phase)
    _print_report(results, args.phase)
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
