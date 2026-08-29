"""Server-rendered screens. The three the Track 3 bar needs, and nothing else.

Phase 8, scoped to `requirements/track3-bar.md`:

1. **Ranked worklist** -- order, amount, and the plain-English reason per row. What makes this not
   an aging report.
2. **Recovery figure** -- causal as the headline, time-window beside it, divergence explained.
3. **Audit log** -- refusals displayed as prominently as sends, hash chain visible, filterable by
   ``recovery_run_id``.

The audit screen is the priority. Any tool can show what it did; showing what it *refused*, with
the rule that stopped it, is the compliance argument -- and it is invisible until it is on screen.

**Read-only.** Nothing here writes, sends, gates or executes. A screen that could act would need
its own authorisation story; a screen that only reads needs none, and the demo does not require
one. Everything else in `agents/frontend.md` stays post-submission.
"""
