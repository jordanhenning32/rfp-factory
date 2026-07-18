"""Quadratic Digital brand tokens for the RFP Factory UI.

Single source of truth for brand colors, typography, and asset paths
referenced from layout.py and (when needed) tab modules. Keep this
small — it's not a CSS framework, just the specific tokens our
design system needs to stay consistent.

Color decisions:
  - NAVY is the primary surface for headers and dark chrome — matches
    the docx export's Heading 1 color and the website's hero section.
  - CYAN is the accent — the parabola arc in the QD logo. Use sparingly
    for action emphasis (primary buttons, active-tab underline,
    focus rings) and not as a default surface.
  - The pre-existing "primary" color used by Quasar widgets across the
    app stays as NAVY for backwards compatibility — Quasar primary
    cascades into hundreds of `color=primary` chip / button props,
    and changing it would mean re-painting every tab. CYAN slots in
    via specific `accent=` overrides.

If the brand updates, change the constants here and re-run
`python scripts/_build_brand_assets.py` to regenerate the matching
header PNG and desktop ICO.
"""

from __future__ import annotations

from pathlib import Path

# ---- Brand colors --------------------------------------------------------

# Primary dark surface — used by the page-frame header and the desktop
# ICO background. RGB: (31, 58, 95).
NAVY = "#1F3A5F"

# Quadratic Digital cyan — the parabola arc in the logo. RGB: (18, 165, 213).
CYAN = "#12A5D5"

# Slightly-darker cyan for hover/pressed states. Approximately
# CYAN multiplied by 0.85 in lightness — gives a clear interactive
# feedback signal without leaving the brand family.
CYAN_HOVER = "#0E8AB3"

# Off-white text for use on NAVY backgrounds. Pure white is too sharp
# at small font sizes; a touch of warmth reads cleaner.
TEXT_ON_DARK = "#F5F7FA"


# ---- Static asset paths --------------------------------------------------
#
# Paths are relative to the project root and assume `assets/` is mounted
# as a NiceGUI static-file route at `/assets` (see app/main.py). Use
# the URL constants in templates and the FILE constants when reading
# the actual bytes (e.g., for ICO resolution at startup).

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = PROJECT_ROOT / "assets"

LOGO_HEADER_FILE = ASSETS_DIR / "brand" / "qd-logo-header.png"
FAVICON_FILE = ASSETS_DIR / "brand" / "favicon.ico"
APP_ICON_FILE = ASSETS_DIR / "rfp_factory.ico"

# URLs match the static-file mount in main.py: `add_static_files('/assets', 'assets')`.
LOGO_HEADER_URL = "/assets/brand/qd-logo-header.png"
FAVICON_URL = "/assets/brand/favicon.ico"


# ---- Tagline -------------------------------------------------------------

TAGLINE = "Exponential value. Delivered."
