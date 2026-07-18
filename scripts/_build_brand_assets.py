"""Generate optimized brand assets from the source logo.

Reads `assets/brand/qd-logo-dark.png` (the source pulled from
quadratic-digital.com) and emits:

  - `assets/brand/qd-logo-header.png` — header-sized PNG (~64px tall)
  - `assets/rfp_factory.ico` — multi-size desktop / favicon ICO

Both are derived from the same source so brand updates only require
re-pulling the source PNG and re-running this script. The desktop
shortcut .lnk references `assets/rfp_factory.ico` directly so it
auto-refreshes after a regen (Windows icon-cache willing).

Run on demand:
    python scripts/_build_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "assets" / "brand" / "qd-logo-dark.png"
HEADER_OUT = PROJECT_ROOT / "assets" / "brand" / "qd-logo-header.png"
ICO_OUT = PROJECT_ROOT / "assets" / "rfp_factory.ico"

# Target heights. Header logo is rendered at ~40px in the page-frame
# header bar; we ship 2x for HiDPI displays. ICO embeds standard
# Windows shell sizes — the OS picks the closest at runtime.
HEADER_HEIGHT_2X = 80
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _trim_transparent_borders(img: Image.Image) -> Image.Image:
    """Crop fully-transparent rows/columns from the image edges so
    the logo fills its rendered box. The source PNG has substantial
    transparent padding which would otherwise eat header space."""
    bbox = img.getbbox()
    return img.crop(bbox) if bbox else img


def _resize_to_height(img: Image.Image, target_h: int) -> Image.Image:
    """Aspect-preserving resize to a target height."""
    w, h = img.size
    new_w = round(w * target_h / h)
    return img.resize((new_w, target_h), Image.LANCZOS)


# Brand navy — same value used by `_theme.NAVY` and the docx export's
# Heading 1. Desktop icons sit on whatever Windows wallpaper the user
# has, so we paint the navy background into the ICO itself; otherwise
# the logo's white wordmark is invisible on light desktops.
ICO_BG = (31, 58, 95, 255)


def _make_square_ico_frame(src: Image.Image, size: int) -> Image.Image:
    """Render one ICO size: navy rounded-square background + the QD
    logo centered on top. Padding scales with size so the icon has
    breathing room from the OS-drawn shortcut arrow."""
    from PIL import ImageDraw

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    pad = max(1, size // 32)
    radius = size // 6
    draw.rounded_rectangle(
        (pad, pad, size - pad - 1, size - pad - 1),
        radius=radius,
        fill=ICO_BG,
    )

    inner = round(size * 0.78)
    src_w, src_h = src.size
    if src_w >= src_h:
        new_w = inner
        new_h = round(src_h * inner / src_w)
    else:
        new_h = inner
        new_w = round(src_w * inner / src_h)
    resized = src.resize((new_w, new_h), Image.LANCZOS)
    x = (size - new_w) // 2
    y = (size - new_h) // 2
    canvas.alpha_composite(resized, (x, y))
    return canvas


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"Source logo missing at {SRC}")

    src = Image.open(SRC).convert("RGBA")
    src_trimmed = _trim_transparent_borders(src)

    # Header PNG — aspect-preserved, height-targeted, kept @2x for HiDPI.
    header = _resize_to_height(src_trimmed, HEADER_HEIGHT_2X)
    header.save(HEADER_OUT, format="PNG", optimize=True)
    print(
        f"  {HEADER_OUT.relative_to(PROJECT_ROOT)} "
        f"({HEADER_OUT.stat().st_size:,} bytes, {header.size[0]}x{header.size[1]})"
    )

    # ICO — multi-size, each rendered fresh from the trimmed source.
    frames = [_make_square_ico_frame(src_trimmed, s) for s in ICO_SIZES]
    primary = frames[-1]
    primary.save(
        ICO_OUT,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=frames[:-1],
    )
    print(
        f"  {ICO_OUT.relative_to(PROJECT_ROOT)} "
        f"({ICO_OUT.stat().st_size:,} bytes, {len(ICO_SIZES)} embedded sizes)"
    )


if __name__ == "__main__":
    main()
