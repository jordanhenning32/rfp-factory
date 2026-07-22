from pathlib import Path
from types import SimpleNamespace

from tests.e2e.conftest import (
    _is_benign_navigation_abort,
    _write_browser_issues,
)


def _request(*, method: str = "GET", failure: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(method=method, failure=failure)


def test_navigation_abort_filter_accepts_only_aborted_gets() -> None:
    assert _is_benign_navigation_abort(_request(failure="net::ERR_ABORTED"))
    assert _is_benign_navigation_abort(
        _request(method="get", failure="net::ERR_ABORTED; frame detached")
    )

    assert not _is_benign_navigation_abort(
        _request(method="POST", failure="net::ERR_ABORTED")
    )
    assert not _is_benign_navigation_abort(
        _request(failure="net::ERR_CONNECTION_RESET")
    )
    assert not _is_benign_navigation_abort(_request())


def test_browser_issue_report_persists_failures(tmp_path: Path) -> None:
    _write_browser_issues(
        tmp_path,
        ["local request failed: GET /asset.js (net::ERR_FAILED)"],
        test_failed=False,
    )
    assert (tmp_path / "issues.txt").read_text(encoding="utf-8") == (
        "- local request failed: GET /asset.js (net::ERR_FAILED)\n"
    )

    _write_browser_issues(tmp_path, [], test_failed=True)
    assert "no browser-side issue was recorded" in (tmp_path / "issues.txt").read_text(
        encoding="utf-8"
    )
