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

    The float division here is display-only and never feeds a calculation.
    """
    if paise >= PAISE_PER_CRORE:
        return f"₹{paise / PAISE_PER_CRORE:.1f}Cr"
    if paise >= PAISE_PER_LAKH:
        return f"₹{paise / PAISE_PER_LAKH:.1f}L"
    return f"₹{_group_indian(paise // PAISE_PER_RUPEE)}"
