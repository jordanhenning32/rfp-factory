from __future__ import annotations

import json

import pytest


def test_payment_cost_basis_update_is_atomic_on_replace_failure(
    monkeypatch, tmp_path,
) -> None:
    import app.services.service_line as service_line

    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    pricing_file = pricing_dir / "payment_systems.json"
    original = {
        "_meta": {"version": "test"},
        "our_cost_basis": {
            "sponsor_acquirer_fee_bps": 5.0,
            "_purpose": "preserve this",
        },
    }
    pricing_file.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(service_line, "DATA_DIR", tmp_path)
    service_line.reload_payment_systems_data()

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(service_line.os, "replace", fail_replace)

    with pytest.raises(OSError, match="synthetic replace failure"):
        service_line.update_payment_cost_basis(sponsor_acquirer_fee_bps=9)

    assert json.loads(pricing_file.read_text(encoding="utf-8")) == original
    assert not list(pricing_dir.glob("*.tmp"))


def test_payment_cost_basis_atomic_update_preserves_metadata(
    monkeypatch, tmp_path,
) -> None:
    import app.services.service_line as service_line

    pricing_dir = tmp_path / "pricing"
    pricing_dir.mkdir()
    pricing_file = pricing_dir / "payment_systems.json"
    original = {
        "_meta": {"version": "test"},
        "our_cost_basis": {
            "sponsor_acquirer_fee_bps": 5.0,
            "_purpose": "preserve this",
        },
    }
    pricing_file.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(service_line, "DATA_DIR", tmp_path)
    service_line.reload_payment_systems_data()

    updated = service_line.update_payment_cost_basis(
        sponsor_acquirer_fee_bps=9,
        confirmed_by_ops_finance=True,
    )

    persisted = json.loads(pricing_file.read_text(encoding="utf-8"))
    assert persisted["our_cost_basis"]["sponsor_acquirer_fee_bps"] == 9.0
    assert persisted["our_cost_basis"]["_confirmed_by_ops_finance"] is True
    assert persisted["our_cost_basis"]["_purpose"] == "preserve this"
    assert updated == persisted["our_cost_basis"]
