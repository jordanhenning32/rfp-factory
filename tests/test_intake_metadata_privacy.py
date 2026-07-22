from __future__ import annotations

import hashlib
import logging

import pytest

from app.agents.intake_metadata import _parse_response


@pytest.mark.parametrize(
    "raw",
    [
        "sensitive solicitation prose without structured output",
        '{"agency": "sensitive buyer", invalid}',
    ],
)
def test_parse_failure_logs_fingerprint_not_raw_text(caplog, raw: str) -> None:
    with caplog.at_level(logging.WARNING):
        result = _parse_response(raw)

    assert not result.has_anything
    assert raw not in caplog.text
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() in caplog.text
