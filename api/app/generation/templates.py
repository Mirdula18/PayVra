"""Deterministic message templates. **The fallback is built first, on purpose.**

agents/agent-engine.md: *"Templates are not a stub. Write four real templates per language per
tone tier. They must be good enough to demo unaided. Write them first, before the LLM path — that
way the fallback is proven by construction rather than hoped for."*

CLAUDE.md invariant 9 is the reason: *the demo must survive an LLM outage*. A fallback written
after the LLM path, to be exercised only when something breaks, is a fallback nobody has read.
These are the messages PAYVRA sends when `LLM_ENABLED=false`, when Groq rate-limits mid-pitch, or
when the validator rejects two drafts in a row — and none of those is a moment to discover the
templates are placeholders.

**The grid is 4 tone tiers x 2 languages x 3 channels = 24 real messages.** The channel axis is
not optional padding: an SMS that is an email body is unusable, and gate check 6 does not care
that the *content* was fine if the message never reads as something a person would send.

Every template renders all five elements ``policy_content.find_missing_elements`` requires — the
exact outstanding amount, the invoice number, the payment link, the opt-out URL, and the sender's
name — and none contains a phrase in ``policy_content.BANNED_PATTERNS``. ``test_templates``
asserts both against every cell of the grid rather than trusting this paragraph.

Money is always :func:`app.money.paise_to_exact`, never ``paise_to_display``. ``₹4.2L`` fails the
required-amount check and cannot be reconciled against a ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.enums import Channel
from app.money import paise_to_exact
from app.schemas.generation import (
    LANGUAGES,
    TONE_TIERS,
    GeneratedMessage,
    Language,
    MessageContext,
    ToneTier,
)

logger = logging.getLogger(__name__)

# Soft ceiling for SMS. Not enforced as a hard failure: a correct message that runs 12 characters
# long is better than a short one missing its opt-out URL, and the required elements are a
# compliance obligation while the segment count is a cost line.
SMS_TARGET_CHARS = 320


def _fmt_date(value: date) -> str:
    """``12 Aug 2026``. Unambiguous to an Indian reader; ``12/08`` and ``08/12`` are not.

    Built by hand rather than with ``%-d``/``%#d``, which differ between POSIX and Windows and
    would render the same invoice two ways depending on where the scheduler happens to run.
    """
    return f"{value.day} {value.strftime('%b %Y')}"


@dataclass(frozen=True)
class Template:
    """One cell of the grid. ``subject`` is ``None`` for every non-email channel."""

    tier: ToneTier
    language: Language
    channel: Channel
    subject: str | None
    body: str

    @property
    def key(self) -> str:
        return f"t{self.tier}-{self.language}-{self.channel.value}"


def opt_out_line(language: Language, opt_out_url: str) -> str:
    """The exact opt-out sentence, in the counterparty's language.

    Public because the drafter puts this same string in the prompt's MUST INCLUDE block: the LLM
    is told to reproduce the line the template would have used, so both paths opt out identically
    and ``find_missing_elements`` sees the URL either way.
    """
    if language == "hinglish":
        return f"Reminders band karne ke liye: {opt_out_url}"
    return f"To stop receiving payment reminders: {opt_out_url}"


def _fields(ctx: MessageContext) -> dict[str, str]:
    """The substitution map. Every value is a string; no template does arithmetic."""
    return {
        "merchant": ctx.merchant_name,
        "counterparty": ctx.counterparty_name,
        "invoice": ctx.invoice_number,
        "amount": paise_to_exact(ctx.outstanding_paise),
        "due_date": _fmt_date(ctx.due_date),
        "days": str(ctx.days_past_due),
        "link": ctx.payment_link_url,
        "opt_out": opt_out_line(ctx.language, ctx.opt_out_url),
        "promise": f"\n{ctx.promise_context}\n" if ctx.promise_context else "",
    }


# =================================================================================================
# ENGLISH
# =================================================================================================

_EN: tuple[Template, ...] = (
    # --- tier 1: courtesy. Assumes oversight. No pressure. ---------------------------------------
    Template(
        1,
        "en",
        Channel.EMAIL,
        subject="Invoice {invoice} — a quick reminder",
        body=(
            "Hi {counterparty},\n"
            "\n"
            "Hope things are going well. This is just a quick note about invoice {invoice} "
            "for {amount}, which was due on {due_date}.\n"
            "{promise}"
            "\n"
            "If it is already in your payment run, please ignore this — nothing further is "
            "needed. Otherwise you can settle it here:\n"
            "\n"
            "{link}\n"
            "\n"
            "If anything about the invoice does not look right, just reply to this email and we "
            "will sort it out.\n"
            "\n"
            "Thanks for your business,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        1,
        "en",
        Channel.SMS,
        subject=None,
        body=(
            "Hi {counterparty}, a quick reminder from {merchant}: invoice {invoice} for "
            "{amount} was due {due_date}. Pay here: {link} — if already paid, please ignore. "
            "{opt_out}"
        ),
    ),
    Template(
        1,
        "en",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "Hi {counterparty} 👋\n"
            "\n"
            "Quick reminder from {merchant} — invoice {invoice} for {amount} was due on "
            "{due_date}.\n"
            "{promise}"
            "\n"
            "You can pay here: {link}\n"
            "\n"
            "Already sent it across? Then please ignore this.\n"
            "\n"
            "{opt_out}"
        ),
    ),
    # --- tier 2: gentle reminder. Warm, but clear that it is overdue. -----------------------------
    Template(
        2,
        "en",
        Channel.EMAIL,
        subject="Invoice {invoice} is now {days} days overdue",
        body=(
            "Hi {counterparty},\n"
            "\n"
            "Following up on invoice {invoice} for {amount}. It was due on {due_date} and is now "
            "{days} days past due.\n"
            "{promise}"
            "\n"
            "We would appreciate it being scheduled in your next payment run. You can pay "
            "directly here:\n"
            "\n"
            "{link}\n"
            "\n"
            "If there is a query holding this up, or you need a copy of the invoice, reply here "
            "and we will get it to you the same day.\n"
            "\n"
            "Best regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        2,
        "en",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant}: invoice {invoice} for {amount} is {days} days overdue (due {due_date}). "
            "Pay: {link}. Reply if there is a query. {opt_out}"
        ),
    ),
    Template(
        2,
        "en",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "Hi {counterparty}, following up from {merchant}.\n"
            "\n"
            "Invoice {invoice} for {amount} was due on {due_date} — that is {days} days ago now.\n"
            "{promise}"
            "\n"
            "Payment link: {link}\n"
            "\n"
            "If something is holding it up, let us know and we will help.\n"
            "\n"
            "{opt_out}"
        ),
    ),
    # --- tier 3: firm. Direct, states business impact. Not aggressive. ----------------------------
    Template(
        3,
        "en",
        Channel.EMAIL,
        subject="Payment required: invoice {invoice}, {days} days overdue",
        body=(
            "Dear {counterparty},\n"
            "\n"
            "Invoice {invoice} for {amount} remains unpaid. It was due on {due_date} and is now "
            "{days} days overdue.\n"
            "{promise}"
            "\n"
            "We have followed up on this more than once. Balances at this age directly affect our "
            "working capital and our ability to service orders, so we do need this settled.\n"
            "\n"
            "Please make payment here:\n"
            "\n"
            "{link}\n"
            "\n"
            "If there is a genuine dispute on this invoice, tell us what it is and we will put "
            "the reminders on hold while we resolve it.\n"
            "\n"
            "Regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        3,
        "en",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant}: invoice {invoice} for {amount} is {days} days overdue and remains "
            "unpaid. Please settle: {link}. If this is disputed, reply and we will hold "
            "reminders. {opt_out}"
        ),
    ),
    Template(
        3,
        "en",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "{counterparty} — invoice {invoice} for {amount} is now {days} days overdue.\n"
            "{promise}"
            "\n"
            "We have followed up a few times on this one. Balances this old affect our working "
            "capital directly, so we do need it cleared.\n"
            "\n"
            "Pay here: {link}\n"
            "\n"
            "Genuine dispute? Tell us and we will pause reminders while it is sorted.\n"
            "\n"
            "— {merchant}\n"
            "{opt_out}"
        ),
    ),
    # --- tier 4: formal notice. References terms, states next steps factually. --------------------
    # "final notice" is permitted from tier 4 only; policy_content tier-gates FAKE_URGENCY at
    # FINAL_NOTICE_MIN_TIER. Below this tier the phrase claims a finality the sequence has not
    # reached, which is exactly what makes it fake.
    Template(
        4,
        "en",
        Channel.EMAIL,
        subject="Final notice — invoice {invoice}, {days} days overdue",
        body=(
            "Dear {counterparty},\n"
            "\n"
            "This is a final notice regarding invoice {invoice} for {amount}, issued against our "
            "agreed payment terms and due on {due_date}. It is now {days} days overdue.\n"
            "{promise}"
            "\n"
            "Despite several reminders, we have not received payment and have had no explanation "
            "for the delay.\n"
            "\n"
            "Unless the balance is settled or you contact us to agree a payment plan, we will "
            "place this account on hold for new orders and refer the balance to our accounts "
            "team for internal review.\n"
            "\n"
            "You can settle it immediately here:\n"
            "\n"
            "{link}\n"
            "\n"
            "If paying in full is not currently possible, reply to this email — we would rather "
            "agree instalments than let this sit unresolved.\n"
            "\n"
            "Regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        4,
        "en",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant} — Final notice: invoice {invoice}, {amount}, {days} days overdue. "
            "Settle now: {link}. Cannot pay in full? Reply to agree instalments. {opt_out}"
        ),
    ),
    Template(
        4,
        "en",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "{counterparty} — final notice on invoice {invoice} for {amount}, now {days} days "
            "past the agreed terms.\n"
            "{promise}"
            "\n"
            "Without payment or an agreed plan, we will hold new orders on this account and "
            "refer the balance for internal review.\n"
            "\n"
            "Settle here: {link}\n"
            "\n"
            "If full payment is not possible right now, reply — instalments are better than "
            "silence.\n"
            "\n"
            "— {merchant}\n"
            "{opt_out}"
        ),
    ),
)


# =================================================================================================
# HINGLISH
# =================================================================================================
# Natural code-mixed Hindi-English in Latin script — the way Indian business people actually write
# on WhatsApp, per prompts/llm-prompts.md. Not formal Hindi, not translated English. Amounts,
# invoice numbers and links stay in English/numerals because that is how they are read aloud and
# how they appear in the counterparty's own ledger.

_HINGLISH: tuple[Template, ...] = (
    # --- tier 1 ----------------------------------------------------------------------------------
    Template(
        1,
        "hinglish",
        Channel.EMAIL,
        subject="Invoice {invoice} — payment reminder",
        body=(
            "Hi {counterparty},\n"
            "\n"
            "Umeed hai sab badhiya chal raha hai. Invoice {invoice} ka {amount} pending hai, "
            "jiski due date {due_date} thi.\n"
            "{promise}"
            "\n"
            "Agar payment already process ho chuki hai to please is message ko ignore kar "
            "dijiye. Warna yahan se seedha pay kar sakte hain:\n"
            "\n"
            "{link}\n"
            "\n"
            "Invoice mein koi dikkat lage to bas reply kar dijiye, hum sort kar denge.\n"
            "\n"
            "Dhanyavaad,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        1,
        "hinglish",
        Channel.SMS,
        subject=None,
        body=(
            "Hi {counterparty}, {merchant} se reminder: invoice {invoice} ka {amount} pending "
            "hai, due date thi {due_date}. Pay karein: {link}. Already paid? Ignore kar dijiye. "
            "{opt_out}"
        ),
    ),
    Template(
        1,
        "hinglish",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "Hi {counterparty} 👋\n"
            "\n"
            "{merchant} se chhota sa reminder — invoice {invoice} ka {amount} pending hai "
            "({due_date} ko due tha).\n"
            "{promise}"
            "\n"
            "Yahan se pay kar sakte hain: {link}\n"
            "\n"
            "Agar bhej diya hai to ignore kar dijiye 🙏\n"
            "\n"
            "{opt_out}"
        ),
    ),
    # --- tier 2 ----------------------------------------------------------------------------------
    Template(
        2,
        "hinglish",
        Channel.EMAIL,
        subject="Invoice {invoice} — {days} din overdue",
        body=(
            "Hi {counterparty},\n"
            "\n"
            "Invoice {invoice} ke baare mein follow up kar rahe hain. Amount {amount} hai, due "
            "date {due_date} thi — matlab ab {days} din ho gaye hain.\n"
            "{promise}"
            "\n"
            "Agar aap ise agle payment run mein laga dein to badi help hogi. Seedha yahan se "
            "pay kar sakte hain:\n"
            "\n"
            "{link}\n"
            "\n"
            "Koi query ya invoice ki copy chahiye ho to reply kijiye — usi din bhej denge.\n"
            "\n"
            "Regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        2,
        "hinglish",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant}: invoice {invoice} ka {amount} ab {days} din overdue hai (due "
            "{due_date}). Pay: {link}. Koi query ho to reply karein. {opt_out}"
        ),
    ),
    Template(
        2,
        "hinglish",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "Hi {counterparty}, {merchant} se follow up.\n"
            "\n"
            "Invoice {invoice} ka {amount} {due_date} ko due tha — ab {days} din ho gaye.\n"
            "{promise}"
            "\n"
            "Payment link: {link}\n"
            "\n"
            "Kuch atka hua hai to bataiye, hum help karenge.\n"
            "\n"
            "{opt_out}"
        ),
    ),
    # --- tier 3 ----------------------------------------------------------------------------------
    Template(
        3,
        "hinglish",
        Channel.EMAIL,
        subject="Payment chahiye: invoice {invoice}, {days} din overdue",
        body=(
            "Dear {counterparty},\n"
            "\n"
            "Invoice {invoice} ka {amount} abhi tak pending hai. Due date {due_date} thi, ab "
            "{days} din overdue ho chuka hai.\n"
            "{promise}"
            "\n"
            "Hum ispar ek se zyada baar follow up kar chuke hain. Itne purane balances se hamare "
            "working capital par seedha asar padta hai aur naye orders service karna mushkil ho "
            "jaata hai — isliye ise clear karna zaroori hai.\n"
            "\n"
            "Payment yahan se kar dijiye:\n"
            "\n"
            "{link}\n"
            "\n"
            "Agar is invoice par genuine dispute hai to bataiye — hum reminders rok denge jab "
            "tak baat resolve nahi ho jaati.\n"
            "\n"
            "Regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        3,
        "hinglish",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant}: invoice {invoice} ka {amount} {days} din overdue hai aur abhi tak "
            "pending hai. Clear karein: {link}. Dispute ho to reply karein, reminders rok denge. "
            "{opt_out}"
        ),
    ),
    Template(
        3,
        "hinglish",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "{counterparty} — invoice {invoice} ka {amount} ab {days} din overdue hai.\n"
            "{promise}"
            "\n"
            "Ispar hum kaafi baar follow up kar chuke hain. Itne purane balances se working "
            "capital par seedha asar padta hai, isliye ise clear karna zaroori hai.\n"
            "\n"
            "Yahan se pay karein: {link}\n"
            "\n"
            "Genuine dispute hai? Bataiye, tab tak reminders rok denge.\n"
            "\n"
            "— {merchant}\n"
            "{opt_out}"
        ),
    ),
    # --- tier 4 ----------------------------------------------------------------------------------
    Template(
        4,
        "hinglish",
        Channel.EMAIL,
        subject="Final notice — invoice {invoice}, {days} din overdue",
        body=(
            "Dear {counterparty},\n"
            "\n"
            "Yeh invoice {invoice} ({amount}) ke liye final notice hai. Hamare agreed payment "
            "terms ke hisaab se iski due date {due_date} thi, aur ab {days} din overdue ho chuka "
            "hai.\n"
            "{promise}"
            "\n"
            "Kai reminders ke baad bhi na payment mili hai, na delay ki koi wajah.\n"
            "\n"
            "Agar balance settle nahi hota ya aap payment plan ke liye contact nahi karte, to "
            "hum is account par naye orders hold kar denge aur balance apni accounts team ke "
            "internal review ke liye bhej denge.\n"
            "\n"
            "Abhi settle karne ke liye:\n"
            "\n"
            "{link}\n"
            "\n"
            "Agar poora amount ek saath dena possible nahi hai to reply kijiye — instalments par "
            "baat kar lete hain, ise aise latka rakhne se behtar hai.\n"
            "\n"
            "Regards,\n"
            "{merchant}\n"
            "\n"
            "{opt_out}"
        ),
    ),
    Template(
        4,
        "hinglish",
        Channel.SMS,
        subject=None,
        body=(
            "{merchant} — Final notice: invoice {invoice}, {amount}, {days} din overdue. Abhi "
            "settle karein: {link}. Poora nahi de sakte? Reply karein, instalments set kar "
            "denge. {opt_out}"
        ),
    ),
    Template(
        4,
        "hinglish",
        Channel.WHATSAPP,
        subject=None,
        body=(
            "{counterparty} — invoice {invoice} ({amount}) par final notice. Agreed terms se "
            "{days} din nikal chuke hain.\n"
            "{promise}"
            "\n"
            "Payment ya agreed plan ke bina hum naye orders hold kar denge aur balance internal "
            "review ke liye bhej denge.\n"
            "\n"
            "Settle karein: {link}\n"
            "\n"
            "Poora amount abhi possible nahi? Reply kijiye — instalments chup rehne se behtar "
            "hain.\n"
            "\n"
            "— {merchant}\n"
            "{opt_out}"
        ),
    ),
)


TEMPLATES: dict[tuple[int, str, str], Template] = {
    (t.tier, t.language, t.channel.value): t for t in (*_EN, *_HINGLISH)
}


class TemplateMissing(LookupError):
    """No template for this tier/language/channel. A programming error, never a runtime one."""


def get(tier: ToneTier, language: Language, channel: Channel) -> Template:
    try:
        return TEMPLATES[(tier, language, channel.value)]
    except KeyError as exc:  # pragma: no cover - the grid is asserted complete by tests
        raise TemplateMissing(f"no template for tier {tier}/{language}/{channel.value}") from exc


def render(ctx: MessageContext, *, fallback_reason: str | None = None) -> GeneratedMessage:
    """Render the template for this context. Deterministic, offline, and always available.

    This is the function CLAUDE.md invariant 9 rests on: it takes no network call, no API key and
    no model, so it cannot fail for any reason the LLM path can fail for. ``fallback_reason`` is
    recorded when a template is used *because* generation failed rather than because it was off,
    which is what lets the audit trail distinguish "LLM disabled" from "LLM produced rubbish".
    """
    template = get(ctx.tone_tier, ctx.language, ctx.channel)
    fields = _fields(ctx)

    body = template.body.format(**fields)
    # Collapse the blank line left behind when there is no promise context to render.
    body = body.replace("\n\n\n", "\n\n").strip()
    subject = template.subject.format(**fields) if template.subject else None

    if ctx.channel is Channel.SMS and len(body) > SMS_TARGET_CHARS:
        # Logged, not truncated. Truncation is what would drop the opt-out URL off the end.
        logger.info(
            "sms template over target invoice=%s chars=%d target=%d",
            ctx.invoice_number,
            len(body),
            SMS_TARGET_CHARS,
        )

    return GeneratedMessage(
        subject=subject,
        body=body,
        tone_tier=ctx.tone_tier,
        language=ctx.language,
        source="template",
        origin=template.key,
        fallback_reason=fallback_reason,
    )


def grid() -> list[tuple[ToneTier, Language, Channel]]:
    """Every combination that must exist. Used by tests to assert the grid is complete."""
    return [(t, lang, ch) for t in TONE_TIERS for lang in LANGUAGES for ch in Channel]


__all__ = [
    "SMS_TARGET_CHARS",
    "TEMPLATES",
    "Template",
    "TemplateMissing",
    "get",
    "grid",
    "opt_out_line",
    "render",
]
