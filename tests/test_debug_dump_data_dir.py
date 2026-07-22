"""Debug payloads follow the active data workspace and use UTC clocks."""
from __future__ import annotations

from datetime import UTC
from datetime import datetime as RealDatetime
from pathlib import Path

from app.agents import compliance_matrix, section_m_extractor


class _UtcClock:
    calls: list[object] = []

    @classmethod
    def now(cls, tz=None):
        cls.calls.append(tz)
        return RealDatetime(2026, 7, 21, 14, 30, 15, 123456, tzinfo=tz)


def test_compliance_debug_dump_uses_active_data_dir(tmp_path: Path, monkeypatch) -> None:
    _UtcClock.calls.clear()
    monkeypatch.setattr(compliance_matrix, "DATA_DIR", tmp_path)
    monkeypatch.setattr(compliance_matrix, "datetime", _UtcClock)

    result = compliance_matrix._dump_failed_payload(
        raw="broken payload",
        filename="RFP source.pdf",
        recursion_depth=2,
        parse_error="invalid JSON",
    )

    assert result is not None
    assert result.parent == tmp_path / "debug" / "compliance_matrix"
    assert result.name == "20260721T143015123456_RFP_source_pdf_d2.txt"
    content = result.read_text(encoding="utf-8")
    assert "broken payload" not in content
    assert "raw payload intentionally omitted" in content
    assert "raw_sha256:" in content
    assert _UtcClock.calls == [UTC]


def test_section_m_debug_dump_uses_active_data_dir(tmp_path: Path, monkeypatch) -> None:
    _UtcClock.calls.clear()
    monkeypatch.setattr(section_m_extractor, "DATA_DIR", tmp_path)
    monkeypatch.setattr(section_m_extractor, "datetime", _UtcClock)

    result = section_m_extractor._dump_failed_payload(
        raw="broken criteria",
        filename="Section M.pdf",
        parse_error="invalid JSON",
    )

    assert result is not None
    assert result.parent == tmp_path / "debug" / "section_m_extractor"
    assert result.name == "20260721T143015123456_Section_M_pdf.txt"
    content = result.read_text(encoding="utf-8")
    assert "broken criteria" not in content
    assert "raw payload intentionally omitted" in content
    assert "raw_sha256:" in content
    assert _UtcClock.calls == [UTC]
