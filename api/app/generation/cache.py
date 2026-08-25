"""Content-hash cache for generated messages (ADR-003, required mitigation).

*"Content-hash cache; never regenerate identical messages."*

The reason is quota, not latency. A 100-invoice batch at three touches is ~300 drafting calls
against a free tier that allows 30 per minute, so a batch that regenerates work it has already
done does not merely run slowly -- it runs out. A rerun after a crash, a demo rehearsed four
times, an operator clicking through the same worklist twice: all of those should cost zero calls.

**The key is the full generation context, not the invoice id.** Anything that would change the
message must change the key, or the cache serves a stale message with the right invoice number and
the wrong amount -- a correctness bug wearing a performance optimisation's clothes. Amount, tier,
language, channel, link, and promise context are all in the key for exactly that reason.

In-process and bounded. A shared Redis would be the right answer for a fleet; this runs as a
single container (ADR-007) and a dictionary that cannot outgrow its bound is one less service to
fail during a demo.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict

from app.schemas.generation import GeneratedMessage, MessageContext

logger = logging.getLogger(__name__)

# Comfortably larger than the ~300 drafts a full batch produces, so a whole run fits with room
# spare, while still bounding memory in a 512 MB container (ADR-003).
MAX_ENTRIES = 1000


def context_key(ctx: MessageContext) -> str:
    """A stable hash of everything that can change the message.

    Deliberately excludes ``invoice_id``: two invoices with identical facts would produce an
    identical message, and there is no reason to pay for that twice. It includes
    ``counterparty_name`` and ``merchant_name`` because both are rendered into the body.
    """
    parts = (
        ctx.merchant_name,
        ctx.counterparty_name,
        ctx.invoice_number,
        str(ctx.outstanding_paise),
        ctx.due_date.isoformat(),
        str(ctx.days_past_due),
        ctx.payment_link_url,
        ctx.opt_out_url,
        ctx.channel.value,
        ctx.language,
        str(ctx.tone_tier),
        str(ctx.touch_count),
        ctx.promise_context or "",
    )
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


class MessageCache:
    """Bounded LRU of validated messages, keyed by content hash.

    Only *validated* messages are ever stored -- see :meth:`put`. Caching a draft before it
    passes the validator would let one bad generation be served repeatedly, turning a transient
    model failure into a persistent one.
    """

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[str, GeneratedMessage] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, ctx: MessageContext) -> GeneratedMessage | None:
        key = context_key(ctx)
        message = self._entries.get(key)
        if message is None:
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        logger.debug("generation cache hit invoice=%s", ctx.invoice_number)
        # A copy, so a caller annotating the result cannot mutate what the next caller receives.
        return message.model_copy(deep=True)

    def put(self, ctx: MessageContext, message: GeneratedMessage) -> None:
        key = context_key(ctx)
        self._entries[key] = message.model_copy(deep=True)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def stats(self) -> dict[str, int]:
        return {"entries": len(self._entries), "hits": self.hits, "misses": self.misses}


# One per process. Tests use `cache.clear()` rather than constructing their own, so that what they
# exercise is the instance the application actually uses.
MESSAGE_CACHE = MessageCache()


__all__ = ["MAX_ENTRIES", "MESSAGE_CACHE", "MessageCache", "context_key"]
