from __future__ import annotations

from scripts.run_e2e_twice import _selection_changing_arg


def test_e2e_gate_allows_reporting_flags() -> None:
    assert _selection_changing_arg(["-vv", "-s", "--tb=short"]) is None


def test_e2e_gate_rejects_subset_and_no_execution_flags() -> None:
    for args in (
        ["-k", "smoke"],
        ["-ksmoke"],
        ["-m=e2e"],
        ["--lf"],
        ["--deselect=tests/e2e/test_workflow.py"],
        ["--collect-only"],
        ["tests/e2e/test_smoke.py"],
    ):
        assert _selection_changing_arg(args) is not None
