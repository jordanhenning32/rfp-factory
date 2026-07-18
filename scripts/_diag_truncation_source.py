"""Diagnose where the truncation in compliance items is coming from.

For each compliance item flagged by the validator as
text_is_truncated_or_incomplete, look at the surrounding text in the
extracted PDF source. If the source has the FULL sentence (the truncation
isn't there), the Sonnet drafter is at fault. If the source already has
the truncated/abbreviated text, it's a pdfplumber extraction artifact.

Run after an intake pipeline finishes:
    python scripts/_diag_truncation_source.py [proposal_id]
"""

import sys

from app.db.session import SessionLocal
from app.models import (
    ComplianceMatrixItem,
    Proposal,
)


def main():
    proposal_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    with SessionLocal() as db:
        prop = db.get(Proposal, proposal_id)
        if prop is None:
            print(f"No proposal with id={proposal_id}")
            return

        # Pull all extracted RFP source text into one searchable string.
        docs = []
        if prop.rfp_package:
            for d in prop.rfp_package.documents:
                if d.extracted_text_md:
                    docs.append((d.filename, d.extracted_text_md))
        if not docs:
            print("No extracted RFP text. Has intake run?")
            return
        all_source = "\n\n".join(t for _, t in docs)
        print(f"Source corpus: {len(all_source):,} chars across {len(docs)} doc(s)")

        # Pull every compliance item's text.
        items = (
            db.query(ComplianceMatrixItem)
            .filter(ComplianceMatrixItem.proposal_id == proposal_id)
            .order_by(ComplianceMatrixItem.id)
            .all()
        )
        print(f"Total compliance items: {len(items)}")

    # Heuristic for "looks truncated":
    #   - text ends in a literal "…" character
    #   - text ends in "..." (3 dots)
    #   - text ends mid-word (last word has only 1-3 chars and isn't a
    #     common short word)
    short_word_whitelist = {
        "as",
        "is",
        "to",
        "be",
        "by",
        "of",
        "in",
        "on",
        "or",
        "if",
        "no",
        "do",
        "an",
        "we",
        "us",
        "it",
        "my",
        "go",
        "I",
        "a",
    }

    def looks_truncated(text: str) -> bool:
        t = text.rstrip()
        if t.endswith("…") or t.endswith("..."):
            return True
        # Final word ends mid-word? (1-3 chars and not in whitelist)
        last = t.split()[-1] if t.split() else ""
        last_clean = last.rstrip(".,;:)]\"'!?")
        if (
            1 <= len(last_clean) <= 3
            and last_clean.lower() not in short_word_whitelist
            and last_clean.isalpha()
        ):
            return True
        return False

    flagged = [it for it in items if looks_truncated(it.requirement_text)]
    print(f"Items with apparent truncation: {len(flagged)}")
    print("=" * 78)

    # For each flagged item, find the truncation marker and look up the
    # last 30 chars before "…" in the source. Report whether the source
    # has the COMPLETE sentence beyond that point (Sonnet's fault) or
    # the truncation appears in the source itself (pdfplumber's fault).
    sonnet_truncs = 0
    source_truncs = 0
    inconclusive = 0

    for it in flagged[:20]:  # limit output to first 20
        text = it.requirement_text.rstrip()
        # Use the last ~30 chars before any trailing "…" / "..." as a
        # search probe.
        probe_end = text.rstrip("…").rstrip(".")
        probe = probe_end[-40:].strip()
        if not probe:
            continue

        # Drop very-short probes (<10 chars) — too noisy
        if len(probe) < 10:
            continue

        # Search the source for this probe
        idx = all_source.find(probe)
        if idx == -1:
            # Probe not found verbatim — Sonnet may have rephrased
            inconclusive += 1
            verdict = "INCONCLUSIVE (probe not in source verbatim)"
            following = "(n/a)"
        else:
            # Look at what follows in the source
            after = all_source[idx + len(probe) : idx + len(probe) + 200]
            after_clean = after.replace("\n", " ").strip()
            if after_clean and not after_clean.startswith(("…", "...")):
                # Source continues normally — Sonnet truncated
                sonnet_truncs += 1
                verdict = "SONNET TRUNCATED (source has more)"
                following = after_clean[:120]
            else:
                source_truncs += 1
                verdict = "SOURCE TRUNCATED (pdfplumber output ends here too)"
                following = after_clean[:120]

        print(f"\n{it.requirement_id}:")
        print(f"  text ends: …{probe[-60:]!r}")
        print(f"  verdict  : {verdict}")
        print(f"  source   : …{following[:120]}…")

    print()
    print("=" * 78)
    print(f"Summary on first {min(20, len(flagged))} flagged items:")
    print(f"  Sonnet truncated:         {sonnet_truncs}")
    print(f"  pdfplumber truncated:     {source_truncs}")
    print(f"  Inconclusive (rephrased): {inconclusive}")


if __name__ == "__main__":
    main()
