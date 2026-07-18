"""Shared layout pieces for all NiceGUI pages.

The page_frame() context manager wraps every page with the standard
Quadratic Digital header (logo + page title + tagline) and the
left-side navigation drawer. Brand tokens come from `_theme.py`.
"""

from __future__ import annotations

from contextlib import contextmanager

from nicegui import ui

from app.core.company_profile import get_profile_version
from app.ui._theme import (
    CYAN,
    LOGO_HEADER_URL,
    NAVY,
    TAGLINE,
    TEXT_ON_DARK,
)

NAV_ITEMS = [
    ("Pipeline", "/", "view_list"),
    ("New Proposal", "/proposals/new", "post_add"),
    ("Knowledge Base", "/kb", "library_books"),
    ("Config", "/config", "tune"),
    ("Admin", "/admin", "monitor_heart"),
]


def _nav_item(label: str, target: str, icon: str) -> None:
    with (
        ui.row()
        .classes(
            "items-center gap-2 p-2 rounded cursor-pointer w-full "
            "transition-colors hover:bg-cyan-50 hover:text-cyan-800"
        )
        .on("click", lambda t=target: ui.navigate.to(t))
    ):
        ui.icon(icon)
        ui.label(label)


@contextmanager
def page_frame(title: str):
    """Render the standard header + sidebar around the page body.

    Header is a dark navy bar matching the QD website hero — the
    logo (white-on-transparent) reads against it cleanly; cyan is
    used as the Quasar accent color so primary buttons / active-tab
    underlines pick up the brand pop without a wholesale recolor of
    every chip and button across the app.
    """
    # Quasar palette: keep `primary` as navy (every existing
    # `color=primary` chip / button across the tabs cascades from
    # this — changing it would re-paint hundreds of widgets).
    # `accent` is the new lever — slot in the QD cyan so action
    # affordances pick up the brand without breaking history.
    ui.colors(primary=NAVY, accent=CYAN)

    with (
        ui.header(elevated=True)
        .classes("items-center justify-between px-6 py-3")
        .style(f"background-color: {NAVY};")
    ):
        # Left cluster: logo + page title.
        with ui.row().classes("items-center gap-4"):
            # Real QD logo. ui.image renders as NiceGUI's q-img-
            # wrapping component; without explicit pixel dimensions
            # via .style() it can collapse to zero size in a flex
            # row. Tailwind's `h-10 w-auto` is unreliable here
            # because the wrapper sets `position: relative` and the
            # inner image is absolute-positioned. Explicit inline
            # height (40px) + width: auto on the wrapper itself
            # forces both wrapper and image to size correctly. The
            # @2x source (80px tall) keeps the result crisp on HiDPI.
            ui.image(LOGO_HEADER_URL).style(
                "height: 40px; width: auto; min-width: 126px; background: transparent;"
            ).props("fit=contain no-spinner")
            ui.label(title).classes("text-base font-medium").style(f"color: {TEXT_ON_DARK};")

        # Right cluster: tagline + profile-version badge.
        with ui.column().classes("gap-0 items-end"):
            ui.label(TAGLINE).classes("text-xs italic").style(f"color: {CYAN};")
            ui.label(f"Quadratic Digital · profile v{get_profile_version()}").classes("text-[10px]").style(
                f"color: {TEXT_ON_DARK}; opacity: 0.7;"
            )

    with ui.left_drawer(value=True, fixed=True).classes("bg-slate-50 p-2 border-r border-slate-200"):
        for label, target, icon in NAV_ITEMS:
            _nav_item(label, target, icon)

    with ui.column().classes("w-full p-6 gap-4"):
        yield
