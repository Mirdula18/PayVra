"""The Phase 5 acceptance criterion, end to end against the database.

*"Done when: with ``LLM_ENABLED=false`` I can generate a message for every invoice in the
worklist top 20, and each one passes validator AND gate check 6."*

Two separate assertions, deliberately. The validator is the generation layer's own check, run at
draft time against the context. Gate check 6 is the compliance boundary, run at send time against
a fresh database read. A message passing one and failing the other would mean the two had drifted
apart -- which is exactly the failure mode the "one phrase list" rule exists to prevent, and the
only way to catch it is to run both over the same message.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.clock import IST
from app.enums import ActionType, Channel, PaymentStatus, RecoveryState
from app.generation import drafter, llm
from app.generation.context import build_context
from app.generation.validator import validate
from app.guardrails.gate import CheckName, check_content_policy, load_context
from app.models.consent import Consent
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.payment_link import PaymentLink
from app.schemas.gate import ProposedAction

pytestmark = pytest.mark.usefixtures("db_available")

MIDDAY_IST = datetime(2026, 8, 24, 12, 0, tzinfo=IST)
BATCH_SIZE = 20


@pytest.fixture(autouse=True)
def _llm_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance criterion is explicitly about ``LLM_ENABLED=false``."""
    monkeypatch.setattr(llm.settings, "llm_enabled", False, raising=False)
    llm.reset()
    from app.generation.cache import MESSAGE_CACHE

    MESSAGE_CACHE.clear()


def _make_book(db: Session, merchant_id: uuid.UUID) -> list[Invoice]:
    """A spread of counterparties, languages, tiers and amounts — not 20 copies of one row.

    The variety is the point: a batch test over identical invoices proves only that one template
    renders. These vary language, tone tier, channel eligibility, amount magnitude (including a
    crore-scale figure, which is where an abbreviated money format would break) and paise
    remainders.
    """
    invoices: list[Invoice] = []
    languages = ("en", "hinglish", "ta", "")  # 'ta' and '' must fall back to English
    amounts = [
        4_999_00,  # small
        1_24_500_00,  # lakh scale
        42_00_000_00,  # tens of lakhs
        1_40_00_000_00,  # crore scale
        87_65_43_21,  # awkward, with paise
    ]
    for i in range(BATCH_SIZE):
        cp = Counterparty(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            name=f"Batch Counterparty {i}",
            name_normalized=f"batch counterparty {i}",
            preferred_language=languages[i % len(languages)],
        )
        db.add(cp)
        db.flush()

        for channel in Channel:
            db.add(
                Consent(
                    id=uuid.uuid4(),
                    counterparty_id=cp.id,
                    channel=channel.value,
                    is_permitted=True,
                    basis="existing_commercial_relationship",
                    granted_at=datetime(2026, 1, 1, tzinfo=UTC),
                    opt_out_token=uuid.uuid4().hex,
                )
            )

        amount = amounts[i % len(amounts)]
        invoice = Invoice(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            counterparty_id=cp.id,
            invoice_number=f"INV-BATCH-{i:04d}",
            amount_paise=amount,
            outstanding_paise=amount,
            issue_date=date(2026, 4, 1),
            due_date=date(2026, 5, 1),
            terms_days=30,
            payment_status=PaymentStatus.UNPAID.value,
            recovery_state=RecoveryState.CHASING.value,
            days_past_due=30 + i * 5,
            current_tone_tier=(i % 4) + 1,
            touch_count=i % 3,
        )
        db.add(invoice)
        db.flush()

        db.add(
            PaymentLink(
                id=uuid.uuid4(),
                invoice_id=invoice.id,
                razorpay_link_id=f"plink_batch_{i}",
                short_url=f"https://rzp.io/i/batch{i:04d}",
                amount_paise=amount,
                reference_id=invoice.invoice_number,
                status="created",
                expire_by=datetime.now(UTC) + timedelta(days=14),
                accept_partial=False,
                idempotency_key=uuid.uuid4().hex,
            )
        )
        invoices.append(invoice)

    db.flush()
    return invoices


def test_every_invoice_in_a_top_20_batch_generates_a_compliant_message(
    db_session: Session, gate_merchant: Any
) -> None:
    """The Phase 5 acceptance criterion.

    Templates only — the LLM is off. Every message must satisfy the generation validator *and*
    gate check 6, the same check that runs immediately before a real send.
    """
    invoices = _make_book(db_session, gate_merchant.id)
    assert len(invoices) == BATCH_SIZE

    seen_languages: set[str] = set()
    seen_tiers: set[int] = set()

    for index, invoice in enumerate(invoices):
        # Rotate the channel so email, SMS and WhatsApp are all exercised across the batch.
        channel = list(Channel)[index % len(Channel)]
        ctx = build_context(db_session, invoice, channel=channel)
        seen_languages.add(ctx.language)
        seen_tiers.add(ctx.tone_tier)

        message = drafter.generate(ctx)
        assert message.source == "template", "LLM_ENABLED=false must yield a template"

        # 1. The generation layer's own validator.
        result = validate(message, ctx)
        assert result.valid, f"{invoice.invoice_number} ({channel.value}): {result.summary}"

        # 2. Gate check 6, over the same message, against a fresh read of the invoice.
        action = ProposedAction(
            invoice_id=invoice.id,
            type=ActionType.SEND_MESSAGE,
            tone_tier=ctx.tone_tier,
            rationale="phase 5 batch acceptance",
            channel=channel,
            message=message.to_draft(ctx),
        )
        gate_ctx = load_context(db_session, action, now=MIDDAY_IST)
        verdict = check_content_policy(gate_ctx, action)
        assert verdict.check is CheckName.CONTENT_POLICY
        assert verdict.passed, f"{invoice.invoice_number} failed gate check 6: {verdict.reason}"

    assert seen_languages == {"en", "hinglish"}, "the batch must exercise both languages"
    assert seen_tiers == {1, 2, 3, 4}, "the batch must exercise all four tone tiers"


def test_an_unsupported_language_falls_back_to_english(
    db_session: Session, gate_merchant: Any
) -> None:
    """FR-8.6 is P2. An untested regional template is worse than an English one."""
    invoices = _make_book(db_session, gate_merchant.id)
    tamil = next(
        inv
        for inv in invoices
        if db_session.get(Counterparty, inv.counterparty_id).preferred_language == "ta"  # type: ignore[union-attr]
    )
    ctx = build_context(db_session, tamil, channel=Channel.EMAIL)
    assert ctx.language == "en"


def test_drafting_refuses_an_invoice_with_no_live_payment_link(
    db_session: Session, gate_merchant: Any, gate_counterparty: Any, gate_invoice: Any
) -> None:
    """A reminder with no link is unpayable, and would fail check 6 anyway. Fail loudly instead."""
    from app.generation.context import ContextIncomplete

    db_session.add(
        Consent(
            id=uuid.uuid4(),
            counterparty_id=gate_counterparty.id,
            channel=Channel.EMAIL.value,
            is_permitted=True,
            basis="existing_commercial_relationship",
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            opt_out_token=uuid.uuid4().hex,
        )
    )
    db_session.flush()

    with pytest.raises(ContextIncomplete, match="no live payment link"):
        build_context(db_session, gate_invoice, channel=Channel.EMAIL)


def test_drafting_refuses_a_counterparty_with_no_consent_record(
    db_session: Session, gate_merchant: Any, gate_invoice: Any
) -> None:
    """No opt-out token means no working opt-out, which is a DPDP problem, not a formatting one."""
    from app.generation.context import ContextIncomplete

    db_session.add(
        PaymentLink(
            id=uuid.uuid4(),
            invoice_id=gate_invoice.id,
            razorpay_link_id="plink_no_consent",
            short_url="https://rzp.io/i/noconsent",
            amount_paise=gate_invoice.outstanding_paise,
            reference_id=gate_invoice.invoice_number,
            status="created",
            expire_by=datetime.now(UTC) + timedelta(days=14),
            accept_partial=False,
            idempotency_key=uuid.uuid4().hex,
        )
    )
    db_session.flush()

    with pytest.raises(ContextIncomplete, match="no whatsapp consent record"):
        build_context(db_session, gate_invoice, channel=Channel.WHATSAPP)
