"""Test the compliance validator's apply-corrections logic without
actually calling Haiku. Monkeypatch validate_compliance_items to
return synthetic ValidationResults at each confidence level, then
verify _validate_and_apply_corrections mutates (or doesn't) the
ExtractedComplianceItem objects correctly.
"""

from unittest import mock

from app.agents.compliance_matrix import ExtractedComplianceItem
from app.agents.compliance_validator import ValidationResult


def main():
    # Build a synthetic batch with mixed correct + incorrect items.
    items = [
        ExtractedComplianceItem(
            requirement_id="REQ-001",
            requirement_text="The contractor shall implement multi-factor authentication.",
            requirement_type="should",  # WRONG — text says shall
            category="technical",
        ),
        ExtractedComplianceItem(
            requirement_id="REQ-002",
            requirement_text="Provide W-9 form and DUNS number.",
            requirement_type="certification",  # WRONG — category, not type
            category="administrative",
        ),
        ExtractedComplianceItem(
            requirement_id="REQ-003",
            requirement_text="Section 3.2 Technical Approach",
            requirement_type="shall",  # but text is just a header
            category="technical",
        ),
        ExtractedComplianceItem(
            requirement_id="REQ-004",
            requirement_text="The vendor shall provide audit logs.",
            requirement_type="shall",  # FINE
            category="technical",
        ),
        ExtractedComplianceItem(
            requirement_id="REQ-005",
            # Bare "Describe X" content prompt with no shall/must in text.
            # Upstream Compliance Matrix Agent had full PDF context and
            # chose `should`. Validator should NOT be allowed to flip to
            # `shall` on visible-text-only reasoning.
            requirement_text="Describe the approach to website design and responsive layouts.",
            requirement_type="should",
            category="technical",
        ),
    ]

    # Synthetic validator output — HIGH on REQ-001, HIGH on REQ-002,
    # MEDIUM on REQ-003 (header concern, no auto-fix), and we don't
    # mention REQ-004 (clean).
    fake_results = [
        ValidationResult(
            requirement_id="REQ-001",
            issue="type_misclassified",
            suggested_type="shall",
            suggested_category=None,
            confidence="HIGH",
            reason="Text says 'shall' but type was 'should'.",
        ),
        ValidationResult(
            requirement_id="REQ-002",
            issue="type_misclassified",
            suggested_type="mandatory_form",
            suggested_category=None,
            confidence="HIGH",
            reason="'certification' is a category; submission of W-9/DUNS is mandatory_form.",
        ),
        ValidationResult(
            requirement_id="REQ-003",
            issue="text_is_a_header_not_a_requirement",
            suggested_type=None,
            suggested_category=None,
            confidence="MEDIUM",
            reason="Looks like a section heading, not a requirement.",
        ),
        # REQ-005: validator (wrongly) flips a bare 'Describe X' from
        # should -> shall at HIGH. The intake guard must BLOCK this
        # because the requirement_text doesn't contain a mandatory verb.
        ValidationResult(
            requirement_id="REQ-005",
            issue="type_misclassified",
            suggested_type="shall",
            suggested_category=None,
            confidence="HIGH",
            reason="Content prompt requiring substantive vendor response.",
        ),
        # Bonus: a result referencing a REQ-ID we don't have, to
        # verify graceful handling.
        ValidationResult(
            requirement_id="REQ-NONEXISTENT",
            issue="other_concern",
            suggested_type=None,
            suggested_category=None,
            confidence="LOW",
            reason="Hypothetical concern.",
        ),
    ]

    from app.jobs import intake as intake_mod

    with (
        mock.patch.object(intake_mod, "validate_compliance_items", return_value=fake_results),
        mock.patch.object(intake_mod, "_set_stage"),
    ):
        # Use a fake proposal_id (-1) that won't match any real row;
        # _set_stage is mocked anyway so no FK risk.
        intake_mod._validate_and_apply_corrections(items, proposal_id=-1)

    print("After validator apply:")
    for it in items:
        print(f"  {it.requirement_id}: type={it.requirement_type!r:<20} category={it.category!r}")

    # Assertions
    assert items[0].requirement_type == "shall", (
        "REQ-001 HIGH-confidence type fix should have applied; got " + items[0].requirement_type
    )
    assert items[1].requirement_type == "mandatory_form", (
        "REQ-002 HIGH-confidence type fix should have applied; got " + items[1].requirement_type
    )
    assert items[2].requirement_type == "shall", (
        "REQ-003 MEDIUM-confidence flag should NOT have mutated the item; got " + items[2].requirement_type
    )
    assert items[3].requirement_type == "shall", "REQ-004 was not flagged; should be unchanged"
    assert items[4].requirement_type == "should", (
        "REQ-005 should NOT have been flipped to 'shall' — bare 'Describe' "
        "imperative with no mandatory verb in text. Intake guard must "
        "block unsupported verb-strictness flips. Got " + items[4].requirement_type
    )
    print("\nAll assertions passed.")
    print("  REQ-001: HIGH applied (should -> shall)              [OK]")
    print("  REQ-002: HIGH applied (certification -> mandatory_form) [OK]")
    print("  REQ-003: MEDIUM logged, NOT applied                   [OK]")
    print("  REQ-004: not flagged, unchanged                       [OK]")
    print("  REQ-005: HIGH BLOCKED (no verb in text, defer upstream) [OK]")
    print("  REQ-NONEXISTENT: gracefully skipped                   [OK]")


if __name__ == "__main__":
    main()
