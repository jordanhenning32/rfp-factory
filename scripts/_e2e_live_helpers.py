"""Shared CLI helpers for live E2E smoke scripts.

These helpers make paid/networked scripts explicit. Deterministic tests can run
freely; anything that calls LLM providers must opt in with --live and must name
the target proposal with --proposal-id or --latest.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable

from sqlalchemy import select

from app.config import get_settings
from app.db.session import session_scope
from app.models import Proposal


def add_live_args(
    parser: argparse.ArgumentParser,
    *,
    allow_stage1_only: bool = False,
) -> None:
    parser.add_argument(
        "--live",
        action="store_true",
        help="Allow paid/networked LLM calls. Required for live stages.",
    )
    if allow_stage1_only:
        parser.add_argument(
            "--stage1-only",
            action="store_true",
            help="Run only the deterministic preflight stage and skip live LLM calls.",
        )
    proposal = parser.add_mutually_exclusive_group()
    proposal.add_argument(
        "--proposal-id",
        type=int,
        help="Proposal id to use for the E2E run.",
    )
    proposal.add_argument(
        "--latest",
        action="store_true",
        help="Use the latest proposal in the configured database.",
    )
    parser.add_argument(
        "legacy_proposal_id",
        nargs="?",
        type=int,
        help=argparse.SUPPRESS,
    )


def require_live(args: argparse.Namespace, *, script_name: str, estimated_cost: str) -> bool:
    if args.live:
        return True
    print(f"!! {script_name} is a live E2E script and was not run.")
    print(f"   Estimated cost: {estimated_cost}")
    print("   Re-run with --live plus --proposal-id N, or --live --latest.")
    return False


def require_api_keys(required: Iterable[str]) -> bool:
    settings = get_settings()
    missing: list[str] = []
    for key in required:
        if key == "anthropic" and not settings.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        elif key == "openai" and not settings.openai_api_key:
            missing.append("OPENAI_API_KEY")
        elif key == "gemini" and not settings.google_or_gemini_key:
            missing.append("GEMINI_API_KEY or GOOGLE_API_KEY")

    if not missing:
        return True

    print("!! missing required API key(s) for this live E2E run:")
    for name in missing:
        print(f"   - {name}")
    print("   Add them to .env or the current process environment, then retry.")
    return False


def pick_proposal_id(args: argparse.Namespace) -> int | None:
    ids = [value for value in (args.proposal_id, args.legacy_proposal_id) if value is not None]
    if len(ids) > 1:
        print("!! pass only one proposal id. Use --proposal-id N with the new CLI.")
        return None
    if ids:
        if args.legacy_proposal_id is not None:
            print("   positional proposal id is deprecated; use --proposal-id next time.")
        return int(ids[0])

    if args.latest:
        with session_scope() as db:
            latest = db.execute(select(Proposal.id, Proposal.title).order_by(Proposal.id.desc()).limit(1)).first()
        if latest is None:
            print("!! no proposals in DB.")
            return None
        print(f"   using latest proposal: id={latest[0]}, title={latest[1]!r}")
        return int(latest[0])

    print("!! choose a proposal with --proposal-id N or --latest.")
    return None
