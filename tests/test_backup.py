from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.backup import create_backup


def _create_complete_source(data_dir: Path) -> None:
    data_dir.mkdir()

    db = sqlite3.connect(data_dir / "sqlite.db")
    db.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO demo(value) VALUES ('ready')")
    db.commit()
    db.close()

    for name in (
        "company_profile.json",
        "teaming_partners.json",
        "decisions.json",
        "internal_pricing_rules.json",
    ):
        (data_dir / name).write_text("{}", encoding="utf-8")
    for dirname in ("pricing", "kb_documents", "rfp_packages", "outputs"):
        (data_dir / dirname).mkdir()
        (data_dir / dirname / "sample.txt").write_text(dirname, encoding="utf-8")


def test_create_backup_includes_canonical_inputs_and_manifest(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    target = tmp_path / "backups"
    _create_complete_source(data_dir)

    dest = create_backup(
        data_dir,
        target,
        now=datetime(2026, 7, 21, 14, 0, tzinfo=UTC),
    )

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["database_integrity_check"] == "ok"
    assert set(manifest["copied_items"]) == {
        "sqlite.db",
        "company_profile.json",
        "teaming_partners.json",
        "decisions.json",
        "internal_pricing_rules.json",
        "pricing",
        "kb_documents",
        "rfp_packages",
        "outputs",
    }
    assert manifest["files"]["sqlite.db"]["sha256"]
    assert (dest / "pricing" / "sample.txt").is_file()
    assert [path for path in target.iterdir()] == [dest]

    restored = sqlite3.connect(dest / "sqlite.db")
    assert restored.execute("SELECT value FROM demo").fetchone()[0] == "ready"
    restored.close()


def test_create_backup_rejects_target_inside_copied_source_tree(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db = sqlite3.connect(data_dir / "sqlite.db")
    db.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY)")
    db.close()
    (data_dir / "rfp_packages").mkdir()

    with pytest.raises(ValueError, match="inside source directory"):
        create_backup(data_dir, data_dir / "rfp_packages" / "backup")


def test_containment_check_does_not_depend_on_source_directory_existing(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _create_complete_source(data_dir)
    (data_dir / "rfp_packages" / "sample.txt").unlink()
    (data_dir / "rfp_packages").rmdir()

    target = data_dir / "rfp_packages" / "backup"
    with pytest.raises(ValueError, match="inside source directory"):
        create_backup(data_dir, target)

    assert not (data_dir / "rfp_packages").exists()


@pytest.mark.parametrize(
    "missing_name",
    ["company_profile.json", "pricing"],
)
def test_missing_required_source_fails_before_target_is_created(
    tmp_path: Path,
    missing_name: str,
) -> None:
    data_dir = tmp_path / "data"
    _create_complete_source(data_dir)
    missing = data_dir / missing_name
    if missing.is_dir():
        (missing / "sample.txt").unlink()
        missing.rmdir()
    else:
        missing.unlink()
    target = tmp_path / "backups"

    with pytest.raises(FileNotFoundError, match=missing_name):
        create_backup(data_dir, target)

    assert not target.exists()


def test_wrong_required_source_type_fails_before_target_is_created(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    _create_complete_source(data_dir)
    pricing = data_dir / "pricing"
    (pricing / "sample.txt").unlink()
    pricing.rmdir()
    pricing.write_text("not a directory", encoding="utf-8")
    target = tmp_path / "backups"

    with pytest.raises(ValueError, match=r"pricing \(expected directory\)"):
        create_backup(data_dir, target)

    assert not target.exists()


def test_copy_failure_does_not_publish_or_leave_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    _create_complete_source(data_dir)
    target = tmp_path / "backups"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("existing", encoding="utf-8")

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated copy failure")

    monkeypatch.setattr("scripts.backup.shutil.copy2", fail_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        create_backup(
            data_dir,
            target,
            now=datetime(2026, 7, 21, 15, 0, tzinfo=UTC),
        )

    assert list(target.iterdir()) == [sentinel]
