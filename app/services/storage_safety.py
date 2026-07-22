"""Fail-closed validation for filesystem paths stored in the database.

Database path fields are untrusted input at deletion time: they may be stale,
manually edited, or left over from a prior workspace.  Destructive services
must prove a target belongs to the active managed-data root before touching
either the database or filesystem.
"""
from __future__ import annotations

from pathlib import Path


class UnsafeManagedPath(ValueError):
    """Raised when a stored path cannot be proven to be application-owned."""


def _resolved_path(path: str | Path | None, *, description: str) -> Path:
    if path is None or (isinstance(path, str) and not path.strip()):
        raise UnsafeManagedPath(
            f"unsafe {description}: the stored path is missing; deletion was blocked"
        )
    try:
        return Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafeManagedPath(
            f"unsafe {description}: the stored path could not be resolved; "
            "deletion was blocked"
        ) from exc


def require_owned_direct_child_directory(
    stored_path: str | Path | None,
    *,
    root: Path,
    expected_name: str,
    description: str,
) -> Path:
    """Return a path only when it is the named direct child of ``root``.

    Resolution is intentionally non-strict so an already-missing, but otherwise
    safe, target does not prevent database cleanup. Existing symlinks/junctions
    resolve to their targets and therefore cannot escape the managed root.
    """
    managed_root = _resolved_path(root, description=f"managed {description} root")
    candidate = _resolved_path(stored_path, description=description)

    if (
        candidate == managed_root
        or candidate.parent != managed_root
        or candidate.name != str(expected_name)
    ):
        raise UnsafeManagedPath(
            f"unsafe {description}: it is not the owned directory for "
            f"{expected_name}; deletion was blocked"
        )
    if candidate.exists() and not candidate.is_dir():
        raise UnsafeManagedPath(
            f"unsafe {description}: the managed target is not a directory; "
            "deletion was blocked"
        )
    return candidate


def require_contained_file(
    stored_path: str | Path | None,
    *,
    root: Path,
    description: str,
    expected_parent_name: str | None = None,
) -> Path:
    """Return a file path only when it is strictly below ``root``.

    The file itself may already be missing. The managed root is never returned,
    and an existing directory is rejected because callers intend to unlink one
    file, not remove a directory tree.
    """
    managed_root = _resolved_path(root, description=f"managed {description} root")
    candidate = _resolved_path(stored_path, description=description)

    try:
        candidate.relative_to(managed_root)
    except ValueError as exc:
        raise UnsafeManagedPath(
            f"unsafe {description}: it is outside the managed directory; "
            "deletion was blocked"
        ) from exc

    if candidate == managed_root:
        raise UnsafeManagedPath(
            f"unsafe {description}: it points at the managed directory itself; "
            "deletion was blocked"
        )
    if (
        expected_parent_name is not None
        and candidate.parent != managed_root / expected_parent_name
    ):
        raise UnsafeManagedPath(
            f"unsafe {description}: it is not inside the owned directory for "
            f"{expected_parent_name}; deletion was blocked"
        )
    if candidate.exists() and not candidate.is_file():
        raise UnsafeManagedPath(
            f"unsafe {description}: the managed target is not a file; "
            "deletion was blocked"
        )
    return candidate
