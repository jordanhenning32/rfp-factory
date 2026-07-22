"""Tests for selecting an isolated data workspace."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import config
from app.services import pricing, service_line


def test_data_dir_defaults_to_project_data(monkeypatch) -> None:
    monkeypatch.delenv("RFP_DATA_DIR", raising=False)

    assert config._resolve_data_dir() == config.PROJECT_ROOT / "data"


def test_data_dir_accepts_absolute_override(tmp_path: Path) -> None:
    workspace = tmp_path / "curated"

    assert config._resolve_data_dir(str(workspace)) == workspace.resolve()


def test_relative_data_dir_is_resolved_from_project_root() -> None:
    assert config._resolve_data_dir("data/demo") == (
        config.PROJECT_ROOT / "data" / "demo"
    ).resolve()


def test_all_data_paths_derive_from_active_data_dir() -> None:
    assert config.KB_DIR == config.DATA_DIR / "kb_documents"
    assert config.RFP_PACKAGES_DIR == config.DATA_DIR / "rfp_packages"
    assert config.OUTPUTS_DIR == config.DATA_DIR / "outputs"
    assert config.BACKUPS_DIR == config.DATA_DIR / "backups"
    assert config.COMPANY_PROFILE_PATH == config.DATA_DIR / "company_profile.json"


def test_isolated_workspace_requires_its_own_sqlite_database(tmp_path: Path) -> None:
    workspace = tmp_path / "isolated"
    matching = f"sqlite:///{(workspace / 'sqlite.db').as_posix()}"
    canonical = f"sqlite:///{(tmp_path / 'canonical.db').as_posix()}"

    config._require_isolated_database(matching, workspace)
    with pytest.raises(ValueError, match="inside that same workspace"):
        config._require_isolated_database(canonical, workspace)


def test_isolated_workspace_rejects_memory_or_external_database(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="inside that same workspace"):
        config._require_isolated_database("sqlite:///:memory:", tmp_path)
    with pytest.raises(ValueError, match="inside that same workspace"):
        config._require_isolated_database(
            "postgresql://example.invalid/rfp", tmp_path
        )


def test_demo_mode_is_explicit() -> None:
    assert config.Settings(
        app_env="demo",
        app_storage_secret="unit-test-demo-secret",
    ).is_demo is True
    assert config.Settings(app_env="development").is_demo is False


@pytest.mark.parametrize(
    "placeholder",
    ["dev-only-change-me", "change-me-to-a-random-string", ""],
)
def test_deployment_rejects_placeholder_storage_secrets(placeholder: str) -> None:
    with pytest.raises(ValueError, match="non-default value"):
        config.Settings(
            app_env="production",
            app_storage_secret=placeholder,
        )


def test_pricing_rules_path_uses_active_data_dir() -> None:
    assert pricing._PRICING_RULES_PATH == (
        config.DATA_DIR / "internal_pricing_rules.json"
    )


def test_service_line_paths_are_mapped_beneath_active_data_dir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_line, "DATA_DIR", tmp_path)

    assert service_line._active_data_path("data/pricing/payment_systems.json") == (
        tmp_path / "pricing" / "payment_systems.json"
    )
