"""Message generation types (agents/agent-engine.md -> Generation, FR-8).

Two objects: the facts a message is written from (:class:`MessageContext`) and the message itself
(:class:`GeneratedMessage`). Both are deliberately plain -- the generation layer must be runnable
without a database session so templates can be tested, rendered, and reviewed on their own.

``MessageContext`` carries the **live** outstanding amount, not a remembered one. Everything
downstream cross-checks against it, and gate check 6 re-reads the invoice again at send time, so a
draft written before a partial payment landed is caught twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.enums import Channel
from app.schemas.gate import DraftMessage

# The two languages FR-8.5 requires. Regional languages are FR-8.6, explicitly P2.
Language = Literal["en", "hinglish"]
LANGUAGES: tuple[Language, ...] = ("en", "hinglish")

# FR-8.1 tone tiers. 1 courtesy, 2 gentle reminder, 3 firm, 4 formal notice.
ToneTier = Literal[1, 2, 3, 4]
TONE_TIERS: tuple[ToneTier, ...] = (1, 2, 3, 4)


@dataclass(frozen=True)
class MessageContext:
    """Everything a message is written from. No session, no ORM objects, no network.

    Frozen because a context is a statement about one moment. A generator that could mutate it
    could quietly change the amount between drafting and validation, and the validator would then
    be checking the message against facts the message itself produced.
    """

    merchant_name: str
    counterparty_name: str
    invoice_number: str
    outstanding_paise: int
    due_date: date
    days_past_due: int
    payment_link_url: str
    opt_out_url: str
    channel: Channel
    language: Language
    tone_tier: ToneTier
    touch_count: int = 0
    # Free text such as "promised to pay by 12 Aug, not received". Rendered verbatim when set.
    promise_context: str | None = None
    invoice_id: uuid.UUID | None = None

    @property
    def is_overdue(self) -> bool:
        return self.days_past_due > 0


class GeneratedMessage(BaseModel):
    """A drafted message and where it came from.

    ``source`` is not decoration. It is what makes the fallback claim auditable: a demo asserting
    "this ran with the LLM switched off" is only checkable if each message says which path wrote
    it, and the audit entry records it alongside the gate verdict.
    """

    subject: str | None = None
    body: str = Field(min_length=1)
    tone_tier: ToneTier
    language: Language
    source: Literal["llm", "template"]
    # Which template produced it, or which model did. Carried into the audit trail.
    origin: str = ""
    # Set when a template was used *because* the LLM path failed, rather than because it was off.
    fallback_reason: str | None = None

    def to_draft(self, ctx: MessageContext) -> DraftMessage:
        """Convert to the gate's :class:`DraftMessage`.

        The quoted fields come from the **context**, not from parsing the body back out. Reading
        them out of the text would mean the gate cross-checks the message against numbers the
        message itself supplied, which validates nothing.
        """
        return DraftMessage(
            channel=ctx.channel,
            subject=self.subject,
            body=self.body,
            quoted_amount_paise=ctx.outstanding_paise,
            quoted_invoice_number=ctx.invoice_number,
            payment_link_url=ctx.payment_link_url,
            opt_out_url=ctx.opt_out_url,
            sender_name=ctx.merchant_name,
        )
