"""The only sanctioned way to send an outbound message.

ADR-005, non-negotiable: *"no code path may send an outbound message without a
``GateVerdict.passed == True`` in scope. Enforce this in the delivery layer's signature, not just
by convention."*

So :func:`send` takes the verdict as a **required** argument and raises without one. A comment
saying "always call gate first" is not a control -- it is a hope. Three things are checked, and
each corresponds to a way the guarantee could otherwise be worked around:

1. **The verdict passed.** The obvious case.
2. **The verdict is for this action.** Otherwise a passing verdict from one invoice could be
   handed to a send for another -- the compliance equivalent of a replay attack, and an easy
   accident in a loop.
3. **The verdict is not stale.** A verdict is a statement about a moment. Check 2 (freshness)
   re-reads payment status precisely because minutes matter, so a verdict from an hour ago is not
   evidence about now.

**The required per-action sequence is generate -> gate -> send, one action at a time.**

Not gate -> generate -> send: check 6 (content policy) validates banned phrases, the payment
link, the opt-out and the outstanding amount, none of which exist until a message is drafted. An
outbound action reaching the gate with ``message=None`` fails check 6 outright -- it does not pass
vacuously -- so gating before generating would block every outbound action, always.

And not "generate the batch, then gate the batch, then send the batch". At NFR-1.6's 4s per
generation, a 100-action dispatch window takes 400s to generate, so verdicts issued at the start
of the batch would be ~6.7 minutes old by the time their sends came round and would fail
:data:`VERDICT_MAX_AGE` for *timing* rather than for policy. Interleaving per action keeps the
gate-to-send gap at roughly zero no matter how large the window is::

    for action in claimed_actions:          # SELECT ... FOR UPDATE SKIP LOCKED
        draft   = generate(action)          # ~4s, LLM, off the request path
        verdict = gate(db, action)          # all seven checks, freshness re-read here
        if not verdict.passed:
            continue                        # already logged as 'blocked' by gate()
        send(action, verdict)               # adjacent to the gate, verdict age ~0
        record_executed(action)             # the 'executed' audit entry, after success

That structure is also why :data:`VERDICT_MAX_AGE` stays at five minutes rather than being raised
to accommodate a batch: the ceiling is not the constraint, the ordering is.

The transport itself (Resend, MSG91, WhatsApp) lands in Phase 4/5. What exists now is the
signature, because retrofitting a mandatory argument after callers exist is how "always call gate
first" becomes a comment.

**Who writes which audit entry.** ``gate()`` writes ``approved`` or ``blocked``. This module
writes ``executed`` -- and only after the transport confirms the send. A gate verdict is
authorisation, not evidence of delivery, and the audit log may under-claim but must never
over-claim.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.clock import IST, now_utc
from app.exceptions import PayvraError
from app.schemas.gate import GateVerdict, ProposedAction

# How long a verdict is evidence for. Comfortably longer than a dispatch batch, far shorter than
# the gap between the 01:30 planning run and the 08:00 window.
VERDICT_MAX_AGE = timedelta(minutes=5)


class GateNotPassedError(PayvraError):
    """An attempt to send without a passing, current, matching gate verdict.

    Never caught and retried. Reaching this means a caller tried to bypass the gate, which is a
    bug in the caller, not a condition to work around.
    """


def assert_sendable(
    action: ProposedAction, verdict: GateVerdict, *, now: datetime | None = None
) -> None:
    """Raise unless this verdict authorises this action, right now.

    Split out from :func:`send` so the Phase 4 transports can assert the same invariant at their
    own entry points without duplicating the reasoning.
    """
    if verdict.invoice_id != action.invoice_id:
        raise GateNotPassedError(
            f"verdict is for invoice {verdict.invoice_id}, not {action.invoice_id}"
        )
    if verdict.action_type is not action.type:
        raise GateNotPassedError(
            f"verdict is for a {verdict.action_type.value} action, not {action.type.value}"
        )
    if not verdict.passed:
        raise GateNotPassedError(f"gate blocked this action: {', '.join(verdict.blocked_by)}")

    moment = now or now_utc()
    age = moment.astimezone(IST) - verdict.evaluated_at.astimezone(IST)
    if age > VERDICT_MAX_AGE:
        raise GateNotPassedError(
            f"gate verdict is {age.total_seconds():.0f}s old, older than the "
            f"{VERDICT_MAX_AGE.total_seconds():.0f}s limit; re-gate before sending"
        )


def send(action: ProposedAction, verdict: GateVerdict, *, now: datetime | None = None) -> None:
    """Send the action's message. **Requires** a passing verdict for this exact action.

    Transport is not implemented -- Phase 4/5 wires Resend, MSG91 and WhatsApp behind this. The
    signature and its precondition exist now so that every future caller is written against them.
    """
    assert_sendable(action, verdict, now=now)
    raise NotImplementedError(
        "delivery transports land in Phase 4/5; the gate precondition above is Phase 3. "
        "The implementation must write the audit entry with outcome='executed' only after the "
        "transport confirms the send -- gate() has already written 'approved'."
    )
