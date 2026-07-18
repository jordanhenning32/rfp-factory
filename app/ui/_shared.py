"""Small UI helpers shared between pages.py and the tabs/ subpackage.

This module holds the few primitives that are used widely enough that
keeping them in pages.py would force every extracted tab module to do
a circular import. Anything substantial belongs in pages.py or a
dedicated tab module — this is not a generic dumping ground.
"""

from __future__ import annotations

from nicegui import ui


def _empty_state(message: str, icon: str = "hourglass_empty") -> None:
    """Centered placeholder card used when a tab/page has no data yet
    (e.g., 'No proposals', 'Compliance items not extracted yet').
    Uses Quasar's xl icon size + opacity 60% for a subdued look that
    doesn't compete with the real content once it lands."""
    with ui.column().classes("items-center justify-center w-full py-16 opacity-60"):
        ui.icon(icon, size="xl")
        ui.label(message).classes("text-base")


def _extract_section_markdown(
    full_md: str,
    section_id: str,
    section_title: str,
) -> str:
    """Pull one section's body out of the compiled corpus by header.
    The compiler emits `## SEC-### — Title` per section; we slice
    between this section's header and the next `## ` header (or EOF).

    Used by the Completed Draft tab and the Final Polish modal preview
    to render one section at a time inside a page-styled wrapper.
    """
    header_marker = f"## {section_id} — {section_title}"
    start = full_md.find(header_marker)
    if start < 0:
        return ""
    body_start = start + len(header_marker)
    # Skip the trailing newline so the body doesn't start with a
    # blank line.
    if body_start < len(full_md) and full_md[body_start] == "\n":
        body_start += 1
    next_header = full_md.find("\n## ", body_start)
    if next_header < 0:
        return full_md[body_start:].rstrip()
    return full_md[body_start:next_header].rstrip()
