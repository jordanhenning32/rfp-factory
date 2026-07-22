"""Coordinate filesystem deletion with a caller-owned database transaction.

Destructive service functions in this application flush database changes but
leave commit/rollback ownership to their callers.  Removing a live file before
that decision makes a later rollback impossible to honor.  This module stages
managed data under a unique quarantine name and binds the final filesystem
action to SQLAlchemy's transaction events:

* rollback renames the quarantined path back to its original location;
* commit permanently purges the quarantined path;
* a post-commit purge failure is logged and reflected in the mutable service
  result, while the data remains under its already-validated quarantine name.

Callers must validate both the original and quarantine paths with their
workspace-specific containment guards.  This helper never accepts an
unvalidated database path on trust.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.orm import Session

PathValidator = Callable[[Path], Path]
PathPurger = Callable[[Path], None]


def _path_exists(path: Path) -> bool:
    """Return whether *path* exists without hiding permission/stat errors."""
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def stage_path_for_transaction(
    db: Session,
    *,
    original_path: Path,
    quarantine_path: Path,
    validate_quarantine: PathValidator,
    purge_quarantine: PathPurger,
    result: dict,
    description: str,
    logger: logging.Logger,
) -> str | None:
    """Stage a managed path and register commit/rollback filesystem actions.

    ``result`` is intentionally mutable.  The service returns it before the
    caller commits; an event callback can therefore report a durable database
    deletion whose post-commit filesystem purge failed, or a deletion that was
    subsequently rolled back.

    Returns ``None`` when staging and event registration succeeded.  Otherwise
    it returns a user-safe reason string; callers must not mutate database rows
    when a reason is returned.
    """
    staged = False
    try:
        if _path_exists(original_path):
            # The unique quarantine path is a sibling, so rename remains on the
            # same filesystem and is atomic on supported application platforms.
            original_path.rename(quarantine_path)
            staged = True
    except Exception:
        logger.exception(
            "failed to stage %s for transactional deletion: %s",
            description,
            original_path,
        )
        return (
            f"failed to stage {description} for deletion; "
            "database records were not changed"
        )

    state = {"settled": False}

    def _after_commit(_session: Session) -> None:
        if state["settled"]:
            return
        state["settled"] = True
        if not staged:
            return
        try:
            safe_quarantine = validate_quarantine(quarantine_path)
            if _path_exists(safe_quarantine):
                purge_quarantine(safe_quarantine)
        except Exception:
            logger.exception(
                "database deletion committed but quarantined %s cleanup failed: %s",
                description,
                quarantine_path,
            )
            try:
                residue_exists = _path_exists(quarantine_path)
            except Exception:
                residue_exists = True
            result["filesystem_cleanup"] = (
                "quarantined" if residue_exists else "incomplete"
            )
            result["cleanup_warning"] = (
                "Database deletion committed, but filesystem cleanup failed; "
                + (
                    "managed data remains safely quarantined."
                    if residue_exists
                    else "manual cleanup may still be required."
                )
            )
            if residue_exists:
                result["quarantine_path"] = str(quarantine_path)

    def _after_rollback(_session: Session) -> None:
        if state["settled"]:
            return
        state["settled"] = True
        result["deleted"] = False
        result["reason"] = "transaction_rolled_back"
        if not staged:
            return
        try:
            safe_quarantine = validate_quarantine(quarantine_path)
            if _path_exists(original_path):
                if _path_exists(safe_quarantine):
                    raise FileExistsError(
                        f"cannot restore {description}: original path already exists"
                    )
                return
            if not _path_exists(safe_quarantine):
                raise FileNotFoundError(
                    f"cannot restore {description}: quarantine path is missing"
                )
            safe_quarantine.rename(original_path)
        except Exception:
            logger.exception(
                "database deletion rolled back but %s restoration failed: %s",
                description,
                quarantine_path,
            )
            result["reason"] = "transaction_rolled_back_filesystem_restore_failed"
            result["filesystem_cleanup"] = "quarantined"
            result["quarantine_path"] = str(quarantine_path)

    commit_registered = False
    rollback_registered = False
    try:
        event.listen(db, "after_commit", _after_commit, once=True)
        commit_registered = True
        event.listen(db, "after_rollback", _after_rollback, once=True)
        rollback_registered = True
    except Exception:
        logger.exception(
            "failed to register transaction hooks for quarantined %s: %s",
            description,
            quarantine_path,
        )
        if commit_registered:
            event.remove(db, "after_commit", _after_commit)
        if rollback_registered:
            event.remove(db, "after_rollback", _after_rollback)
        if staged:
            try:
                safe_quarantine = validate_quarantine(quarantine_path)
                if _path_exists(safe_quarantine) and not _path_exists(original_path):
                    safe_quarantine.rename(original_path)
            except Exception:
                logger.exception(
                    "failed to restore %s after transaction-hook setup failure: %s",
                    description,
                    quarantine_path,
                )
        return (
            f"failed to prepare {description} deletion transaction; "
            "database records were not changed"
        )

    return None
