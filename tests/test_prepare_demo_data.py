from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.prepare_demo_data import build_demo_database, prepare_demo_bundle


def _seed_source(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE rfp_packages (
            id INTEGER PRIMARY KEY,
            storage_dir TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY,
            rfp_package_id INTEGER NOT NULL
                REFERENCES rfp_packages(id) ON DELETE RESTRICT,
            title TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER NOT NULL
                REFERENCES proposals(id) ON DELETE CASCADE
        );
        CREATE TABLE rfp_package_documents (
            id INTEGER PRIMARY KEY,
            rfp_package_id INTEGER NOT NULL
                REFERENCES rfp_packages(id) ON DELETE CASCADE,
            storage_path TEXT NOT NULL
        );
        CREATE TABLE knowledge_base_documents (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL
        );
        CREATE TABLE knowledge_base_chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL
                REFERENCES knowledge_base_documents(id) ON DELETE CASCADE
        );
        CREATE TABLE profile_suggestions (
            id INTEGER PRIMARY KEY,
            kb_document_id INTEGER NOT NULL
                REFERENCES knowledge_base_documents(id) ON DELETE CASCADE
        );
        CREATE TABLE learned_rules (id INTEGER PRIMARY KEY);
        CREATE TABLE proposal_sections (
            id INTEGER PRIMARY KEY,
            proposal_id INTEGER NOT NULL
                REFERENCES proposals(id) ON DELETE CASCADE,
            citations_json TEXT NOT NULL
        );
        CREATE TABLE reviewer_findings (
            id INTEGER PRIMARY KEY,
            proposal_section_id INTEGER NOT NULL
                REFERENCES proposal_sections(id) ON DELETE CASCADE,
            finding_text TEXT NOT NULL
        );

        INSERT INTO rfp_packages(id) VALUES (4), (6), (9);
        INSERT INTO proposals(id, rfp_package_id, title, status) VALUES
            (4, 4, 'Artifact story', 'draft_ready'),
            (6, 6, 'Approval story', 'awaiting_outline_approval'),
            (9, 9, 'Old clutter', 'archived');
        INSERT INTO agent_runs(id, proposal_id) VALUES (1, 4), (2, 9);
        INSERT INTO rfp_package_documents(id, rfp_package_id, storage_path) VALUES
            (1, 4, 'source/rfp_packages/4/four.pdf'),
            (2, 6, 'source/rfp_packages/6/six.docx'),
            (3, 9, 'source/rfp_packages/9/nine.pdf');
        INSERT INTO knowledge_base_documents(id, filename)
            VALUES (1, 'Ordinary Reference.pdf');
        INSERT INTO knowledge_base_chunks(id, document_id) VALUES (1, 1);
        INSERT INTO profile_suggestions(id, kb_document_id) VALUES (1, 1);
        INSERT INTO learned_rules(id) VALUES (1);
        """
    )
    db.commit()
    db.close()


def test_build_demo_database_scrubs_exact_kb_filenames_from_retained_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "demo.sqlite"
    _seed_source(source)
    sensitive_filename = "Confidential Acme Case Study.PDF"
    second_filename = "Private_Team_Experience.docx"
    normal_text = "Acme case-study methods remain part of the proposal narrative."

    db = sqlite3.connect(source)
    db.execute(
        "UPDATE knowledge_base_documents SET filename=? WHERE id=1",
        (sensitive_filename,),
    )
    db.execute(
        "INSERT INTO knowledge_base_documents(id, filename) VALUES (2, ?)",
        (second_filename,),
    )
    citations = json.dumps(
        [
            {
                "claim": normal_text,
                "source_kb_doc": "confidential acme case study.pdf",
            },
            {
                "claim": "Staffing evidence",
                "source_kb_doc": second_filename.upper(),
            },
        ]
    )
    db.execute(
        "INSERT INTO proposal_sections(id, proposal_id, citations_json) "
        "VALUES (1, 4, ?)",
        (citations,),
    )
    db.execute(
        "INSERT INTO reviewer_findings(id, proposal_section_id, finding_text) "
        "VALUES (1, 1, ?)",
        (
            f"Verify {sensitive_filename.upper()} and {second_filename}; "
            f"{normal_text}",
        ),
    )
    db.commit()
    db.close()
    source_bytes_before = source.read_bytes()

    summary = build_demo_database(source, output, keep_proposal_ids=[4])

    assert summary["scrubbed_kb_filename_references"] == 4
    assert source.read_bytes() == source_bytes_before

    db = sqlite3.connect(output)
    finding_text = db.execute(
        "SELECT finding_text FROM reviewer_findings WHERE id=1"
    ).fetchone()[0]
    citations_json = db.execute(
        "SELECT citations_json FROM proposal_sections WHERE id=1"
    ).fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM knowledge_base_documents").fetchone()[0] == 0
    db.close()

    parsed_citations = json.loads(citations_json)
    retained_values = f"{finding_text}\n{citations_json}".casefold()
    assert sensitive_filename.casefold() not in retained_values
    assert second_filename.casefold() not in retained_values
    assert retained_values.count("[curated kb source]") == 4
    assert normal_text in finding_text
    assert parsed_citations[0]["claim"] == normal_text

    source_db = sqlite3.connect(source)
    source_values = "\n".join(
        str(row[0])
        for row in source_db.execute(
            "SELECT filename FROM knowledge_base_documents ORDER BY id"
        )
    )
    source_finding = source_db.execute(
        "SELECT finding_text FROM reviewer_findings WHERE id=1"
    ).fetchone()[0]
    source_db.close()
    assert sensitive_filename in source_values
    assert second_filename in source_values
    assert sensitive_filename.upper() in source_finding


def test_build_demo_database_prunes_to_allowlist_and_removes_kb(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "demo.sqlite"
    _seed_source(source)

    summary = build_demo_database(
        source,
        output,
        keep_proposal_ids=[6, 4, 4],
    )

    assert source.exists()
    assert summary["source_unchanged"] is True
    assert [p["id"] for p in summary["retained_proposals"]] == [4, 6]
    assert summary["integrity_check"] == "ok"
    assert summary["counts"]["agent_runs"] == 1
    assert summary["counts"]["knowledge_base_documents"] == 0

    db = sqlite3.connect(output)
    assert db.execute("SELECT id FROM proposals ORDER BY id").fetchall() == [(4,), (6,)]
    assert db.execute("SELECT id FROM rfp_packages ORDER BY id").fetchall() == [(4,), (6,)]
    assert db.execute("SELECT COUNT(*) FROM knowledge_base_chunks").fetchone()[0] == 0
    assert list(db.execute("PRAGMA foreign_key_check")) == []
    db.close()


def test_build_demo_database_refuses_missing_id_without_leaving_output(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "demo.sqlite"
    _seed_source(source)

    with pytest.raises(ValueError, match="not found"):
        build_demo_database(source, output, keep_proposal_ids=[404])

    assert not output.exists()


def test_build_demo_database_requires_replace_for_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    output = tmp_path / "demo.sqlite"
    _seed_source(source)
    output.write_bytes(b"do not overwrite")

    with pytest.raises(FileExistsError, match="--replace"):
        build_demo_database(source, output, keep_proposal_ids=[4])

    assert output.read_bytes() == b"do not overwrite"


def test_prepare_demo_bundle_copies_and_rewrites_file_storage(tmp_path: Path) -> None:
    source_data = tmp_path / "source-data"
    demo_data = tmp_path / "demo-data"
    source_data.mkdir()
    _seed_source(source_data / "sqlite.db")
    for filename in (
        "company_profile.json",
        "teaming_partners.json",
        "decisions.json",
        "internal_pricing_rules.json",
    ):
        (source_data / filename).write_text("{}", encoding="utf-8")
    (source_data / "pricing").mkdir()
    (source_data / "pricing" / "payment_systems.json").write_text(
        "{}", encoding="utf-8",
    )
    for package_id, filename in ((4, "four.pdf"), (6, "six.docx"), (9, "nine.pdf")):
        package_dir = source_data / "rfp_packages" / str(package_id)
        package_dir.mkdir(parents=True)
        (package_dir / filename).write_text(str(package_id), encoding="utf-8")

    summary = prepare_demo_bundle(
        source_data,
        demo_data,
        keep_proposal_ids=[4, 6],
    )

    assert summary["isolated_storage_paths"] is True
    assert summary["copied_rfp_packages"] == 2
    assert summary["complete"] is True
    manifest = json.loads(
        (demo_data / "demo_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["complete"] is True
    assert manifest["database_sha256"] == summary["database_sha256"]
    assert manifest["database_sha256"] == hashlib.sha256(
        (demo_data / "sqlite.db").read_bytes()
    ).hexdigest()
    assert manifest["size_bytes"] == summary["size_bytes"]
    assert manifest["size_bytes"] == (demo_data / "sqlite.db").stat().st_size
    assert not (demo_data / "rfp_packages" / "9").exists()
    assert (demo_data / "rfp_packages" / "4" / "four.pdf").is_file()
    assert (demo_data / "kb_documents").is_dir()

    db = sqlite3.connect(demo_data / "sqlite.db")
    paths = [Path(row[0]) for row in db.execute(
        "SELECT storage_path FROM rfp_package_documents ORDER BY id"
    )]
    db.close()
    assert paths == [
        demo_data.resolve() / "rfp_packages" / "4" / "four.pdf",
        demo_data.resolve() / "rfp_packages" / "6" / "six.docx",
    ]


def test_prepare_demo_bundle_refuses_unowned_nonempty_output(tmp_path: Path) -> None:
    source_data = tmp_path / "source-data"
    unrelated = tmp_path / "unrelated-workspace"
    source_data.mkdir()
    unrelated.mkdir()
    _seed_source(source_data / "sqlite.db")
    protected = unrelated / "pricing" / "keep.txt"
    protected.parent.mkdir()
    protected.write_text("do not delete", encoding="utf-8")

    with pytest.raises(ValueError, match="ownership marker"):
        prepare_demo_bundle(
            source_data,
            unrelated,
            keep_proposal_ids=[4],
            replace=True,
        )

    assert protected.read_text(encoding="utf-8") == "do not delete"


@pytest.mark.parametrize(
    "relative_target",
    (
        Path("pricing") / "nested-demo",
        Path("rfp_packages") / "4" / "nested-demo",
    ),
)
def test_prepare_demo_bundle_refuses_target_inside_managed_source_tree(
    tmp_path: Path,
    relative_target: Path,
) -> None:
    source_data = tmp_path / "source-data"
    source_data.mkdir()
    _seed_source(source_data / "sqlite.db")
    output_data = source_data / relative_target
    output_data.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="managed canonical data subtree"):
        prepare_demo_bundle(
            source_data,
            output_data,
            keep_proposal_ids=[4],
        )

    assert not output_data.exists()


def test_prepare_demo_bundle_refuses_canonical_file_as_output_directory(
    tmp_path: Path,
) -> None:
    source_data = tmp_path / "source-data"
    source_data.mkdir()
    _seed_source(source_data / "sqlite.db")
    canonical_file = source_data / "company_profile.json"
    canonical_file.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="canonical data file"):
        prepare_demo_bundle(
            source_data,
            canonical_file,
            keep_proposal_ids=[4],
        )

    assert canonical_file.read_text(encoding="utf-8") == "{}"


def test_prepare_demo_bundle_refuses_descendant_of_canonical_file_path(
    tmp_path: Path,
) -> None:
    source_data = tmp_path / "source-data"
    source_data.mkdir()
    _seed_source(source_data / "sqlite.db")
    canonical_file = source_data / "company_profile.json"
    canonical_file.write_text("do not alter", encoding="utf-8")
    output_data = canonical_file / "nested-demo"

    with pytest.raises(ValueError, match="descend from a canonical data file"):
        prepare_demo_bundle(
            source_data,
            output_data,
            keep_proposal_ids=[4],
        )

    assert canonical_file.read_text(encoding="utf-8") == "do not alter"
    assert not output_data.exists()
