"""Money helpers. Money is ``int`` paise everywhere; format only at the presentation boundary.

Never store or compute money as ``float`` or ``Decimal`` in the DB layer.
"""

from __future__ import annotations

PAISE_PER_RUPEE = 100
PAISE_PER_LAKH = 100 * 100_000  # 1 lakh rupees
PAISE_PER_CRORE = 100 * 1_00_00_000  # 1 crore rupees


def _group_indian(rupees: int) -> str:
    """Format a whole-rupee integer with Indian digit grouping (e.g. 12,34,567)."""
    s = str(rupees)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    parts.insert(0, head)
    return ",".join(parts) + "," + tail


def paise_to_display(paise: int) -> str:
    """Render paise as Indian currency: ``₹1,200`` / ``₹4.2L`` / ``₹1.4Cr``.

    Dashboard formatting. **Never put this in an outbound message** -- see
    :func:`paise_to_exact` for why.

    The float division here is display-only and never feeds a calculation.
    """
    if paise >= PAISE_PER_CRORE:
        return f"₹{paise / PAISE_PER_CRORE:.1f}Cr"
    if paise >= PAISE_PER_LAKH:
        return f"₹{paise / PAISE_PER_LAKH:.1f}L"
    return f"₹{_group_indian(paise // PAISE_PER_RUPEE)}"


def paise_to_exact(paise: int) -> str:
    """Render paise in full, unabbreviated: ``₹1,200`` / ``₹4,20,000`` / ``₹1,24,500.50``.

    **This is the only money format an outbound message may use.** The abbreviated form is a
    real correctness hazard here, not a style preference: ``paise_to_display(42000000)`` is
    ``₹4.2L``, whose digits are ``42`` -- so ``policy_content._amount_appears`` cannot find the
    outstanding amount, and gate check 6 blocks the send for a missing required element. A
    counterparty also cannot reconcile "₹4.2L" against their ledger.

    Paise are shown only when non-zero, because "₹1,200.00" reads like a system printout while
    "₹1,200" reads like a person wrote it.
    """
    rupees, remainder = divmod(paise, PAISE_PER_RUPEE)
    grouped = _group_indian(rupees)
    return f"₹{grouped}.{remainder:02d}" if remainder else f"₹{grouped}"
