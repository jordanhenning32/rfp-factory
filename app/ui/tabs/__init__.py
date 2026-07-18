"""Proposal Review tab renderers.

Each module in this subpackage exports the `_render_*_tab` function
for one tab on the Proposal Review page. Extracted from
`app/ui/pages.py` so each tab is grep-able and self-contained.

Add a new tab by:
1. Creating a new module here with `_render_<name>_tab(proposal_id, ...)`
2. Importing it in `pages.py`
3. Adding the entry to `_PROPOSAL_REVIEW_TABS` and (if it has a badge)
   the count query in `_compute_tab_badges`.
"""
