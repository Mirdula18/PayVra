"""Deterministic seed builder.

Produces the exact dataset in ``agents/data-and-seed.md``: 120 invoices across 34 counterparties
in all seven archetypes, 60 days of backdated history, the Hinglish reply verbatim, 6 partials,
4 settled, 4 MSME-45 crossings, 8 defective raw rows (emitted as an ingestion fixture), and the
four seeded blocked audit entries. Fixed ``RANDOM_SEED`` fixes the shape; all dates anchor to
``today()`` so the batch is always correctly aged.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from app.audit.log import record, verify_chain
from app.clock import IST, today
from app.db import SessionLocal, engine
from app.enums import (
    ActionStatus,
    ActionType,
    ActorType,
    Channel,
    PaymentStatus,
    RecoveryState,
    ReplyIntent,
    StopReason,
    UnpaidCause,
)
from app.metrics import collection_period_days, mean_days_past_due, quantize_days
from app.models import (
    Action,
    AuditLog,
    Consent,
    Contact,
    Counterparty,
    Invoice,
    Merchant,
    Message,
    MetricsSnapshot,
    Promise,
    Reply,
)
from app.seed import data
from app.seed.data import (
    ARCHETYPES,
    EXCLUDED_NAME,
    GATE_CHECKS,
    HINGLISH_NAMES,
    HINGLISH_REPLY,
    HINGLISH_REPLY_NAME,
    MERCHANT_EMAIL,
    MERCHANT_NAME,
    MSME_NAMES,
    QUARANTINED_NAME,
    RANDOM_SEED,
)

INVOICE_TARGET = 120
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
# builder.py -> seed -> app -> api/; alembic.ini sits at api/alembic.ini.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

# Correlation between invoice size and age; tuned so the amount-weighted collection period lands
# near the 73-day headline (docs/vision.md). See _build_invoices.
_AGE_AMOUNT_ALPHA = 0.22

# Global aging-bucket pool over [current, 0-30, 31-60, 61-90, 90+]; sums to INVOICE_TARGET.
# Assigned youngest-first by archetype so the 40/22/18/12/8 distribution is exact, not approximate.
_BUCKET_POOL = [48, 26, 22, 14, 10]
_ARCHETYPE_AGE_SCORE: dict[str, int] = {
    "reliable_late": 0,
    "wrong_contact": 1,
    "disputer": 2,
    "chronic_slow": 3,
    "cash_crunched": 4,
    "promise_breaker": 5,
    "ghost": 6,
}
_BUCKET_DPD = {
    0: (-20, 0),  # current (not yet due)
    1: (1, 30),
    2: (31, 60),
    3: (61, 90),
    4: (91, 150),
}
_CAUSE_BY_ARCHETYPE: dict[str, UnpaidCause] = {
    "reliable_late": UnpaidCause.OVERSIGHT,
    "chronic_slow": UnpaidCause.OVERSIGHT,
    "cash_crunched": UnpaidCause.CASH_CRUNCH,
    "promise_breaker": UnpaidCause.CASH_CRUNCH,
    "disputer": UnpaidCause.DISPUTE,
    "wrong_contact": UnpaidCause.WRONG_CONTACT,
    "ghost": UnpaidCause.UNKNOWN,
}
_SUFFIX_RE = re.compile(
    r"\b(pvt|private|ltd|limited|llp|co|company|works|traders|enterprises|industries)\b"
)


def _det_uuid(rng: random.Random) -> uuid.UUID:
    return uuid.UUID(int=rng.getrandbits(128))


def _ist_dt(d: date, hour: int = 10, minute: int = 0) -> datetime:
    """Aware UTC datetime for a given IST wall-clock date/time."""
    return datetime.combine(d, time(hour, minute), tzinfo=IST).astimezone(UTC)


def _normalize_name(name: str) -> str:
    lowered = name.lower().replace("&", " ").replace(".", " ")
    lowered = _SUFFIX_RE.sub(" ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _fake_gstin(rng: random.Random) -> str:
    """Structurally plausible but non-existent GSTIN (never a real one)."""
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    digits = "0123456789"
    state = f"{rng.randint(10, 37):02d}"
    pan = (
        "".join(rng.choice(letters) for _ in range(5))
        + "".join(rng.choice(digits) for _ in range(4))
        + rng.choice(letters)
    )
    return f"{state}{pan}{rng.choice(digits)}Z{rng.choice(letters + digits)}"


def _mobile(i: int) -> str:
    # Reserved-looking +91 99999 xxxxx range; never a real subscriber number.
    return f"+9199999{i:05d}"


def _lognormal_paise(rng: random.Random) -> int:
    """Log-normal amount in [₹18,000, ₹14,00,000], median ≈ ₹1.8L."""
    lo, hi = 1_800_000, 140_000_000  # paise
    value = int(round(rng.lognormvariate(mu=16.7, sigma=0.9)))
    value = max(lo, min(hi, value))
    return value - (value % 100)  # whole rupees


def _terms_days(rng: random.Random) -> int:
    # 82% on 0-30 day terms (Recordent finding); the rest longer.
    if rng.random() < 0.82:
        return rng.choice([15, 30])
    return rng.choice([45, 60, 90])


def _bucket_of(dpd: int) -> str:
    if dpd <= 0:
        return "current"
    if dpd <= 30:
        return "0-30"
    if dpd <= 60:
        return "31-60"
    if dpd <= 90:
        return "61-90"
    return "90+"


class SeedBuilder:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.rng = random.Random(RANDOM_SEED)
        self.anchor = today()
        self.merchant: Merchant
        self.counterparties: list[Counterparty] = []
        self.cp_archetype: dict[uuid.UUID, str] = {}
        self.primary_contact: dict[uuid.UUID, Contact] = {}
        self.cp_by_name: dict[str, Counterparty] = {}
        self.invoices: list[Invoice] = []
        self.invoices_by_cp: dict[uuid.UUID, list[Invoice]] = {}

    # -- lifecycle ---------------------------------------------------------------------------
    def build(self) -> None:
        self._build_merchant()
        self._build_counterparties()
        self._build_invoices()
        self._roll_up_counterparties()
        self._build_history()
        self._build_blocked_audit()
        self._build_metrics()
        self._write_messy_fixture()

    # -- merchant ----------------------------------------------------------------------------
    def _build_merchant(self) -> None:
        self.merchant = Merchant(
            id=_det_uuid(self.rng),
            name=MERCHANT_NAME,
            email=MERCHANT_EMAIL,
            razorpay_key_id="rzp_test_seeded",
            is_paused=False,
            created_at=_ist_dt(self.anchor - timedelta(days=90)),
        )
        self.db.add(self.merchant)
        self.db.flush()

    # -- counterparties ----------------------------------------------------------------------
    def _build_counterparties(self) -> None:
        # Two passes: counterparties are flushed to the DB before their contacts/consents so the
        # FK targets exist (there are no ORM relationships to drive insert ordering).
        cp_archetypes: list[str] = []
        for archetype in ARCHETYPES:
            for name in archetype.names:
                cp = Counterparty(
                    id=_det_uuid(self.rng),
                    merchant_id=self.merchant.id,
                    name=name,
                    name_normalized=_normalize_name(name),
                    gstin=_fake_gstin(self.rng),
                    is_msme=name in MSME_NAMES,
                    preferred_language="hinglish" if name in HINGLISH_NAMES else "en",
                    lifetime_revenue_paise=0,
                    broken_promise_count=0,
                    is_quarantined=name == QUARANTINED_NAME,
                    is_excluded=name == EXCLUDED_NAME,
                    created_at=_ist_dt(self.anchor - timedelta(days=self.rng.randint(120, 400))),
                )
                self.db.add(cp)
                self.counterparties.append(cp)
                self.cp_archetype[cp.id] = archetype.key
                self.cp_by_name[name] = cp
                cp_archetypes.append(archetype.key)
        self.db.flush()

        for idx, (cp, archetype_key) in enumerate(
            zip(self.counterparties, cp_archetypes, strict=True)
        ):
            self._build_contacts_and_consents(cp, archetype_key, idx)
        self.db.flush()

    def _build_contacts_and_consents(self, cp: Counterparty, archetype_key: str, idx: int) -> None:
        roles = ["ap_head", "accounts", "owner"]
        n_contacts = 1 if archetype_key == "ghost" else self.rng.randint(1, 2)
        for c in range(n_contacts):
            is_primary = c == 0
            # Wrong-contact archetype: the primary email is dead and goes stale on bounce.
            stale = archetype_key == "wrong_contact" and is_primary
            local = _normalize_name(cp.name).replace(" ", ".")[:24]
            contact = Contact(
                id=_det_uuid(self.rng),
                counterparty_id=cp.id,
                name=self.rng.choice(
                    ["Ramesh Iyer", "Anita Rao", "Vikram Shah", "Priya Nair", "Suresh Menon"]
                ),
                email=f"{local}.{c}@{'invalid-mx' if stale else 'example'}.co.in",
                phone=_mobile(idx * 2 + c),
                role=roles[min(c, len(roles) - 1)],
                is_primary=is_primary,
                is_stale=stale,
                created_at=cp.created_at,
            )
            self.db.add(contact)
            if is_primary:
                self.primary_contact[cp.id] = contact

        granted = _ist_dt(cp.created_at.astimezone(IST).date())
        for channel in (Channel.EMAIL, Channel.SMS, Channel.WHATSAPP):
            # Quarantined accounts have no consent basis; others rely on the relationship.
            permitted = not cp.is_quarantined
            revoked = None
            if cp.is_excluded and channel == Channel.EMAIL:
                permitted = False
                revoked = _ist_dt(self.anchor - timedelta(days=self.rng.randint(5, 40)))
            self.db.add(
                Consent(
                    id=_det_uuid(self.rng),
                    counterparty_id=cp.id,
                    channel=channel.value,
                    is_permitted=permitted,
                    basis="none_on_file"
                    if cp.is_quarantined
                    else "existing_commercial_relationship",
                    granted_at=granted,
                    revoked_at=revoked,
                    opt_out_token=uuid.UUID(int=self.rng.getrandbits(128)).hex,
                )
            )

    # -- invoices ----------------------------------------------------------------------------
    def _assign_counts(self) -> list[int]:
        counts = [self.rng.randint(2, 5) for _ in self.counterparties]
        # Nudge to exactly INVOICE_TARGET, keeping every counterparty in [1, 6].
        while sum(counts) != INVOICE_TARGET:
            i = self.rng.randrange(len(counts))
            if sum(counts) < INVOICE_TARGET and counts[i] < 6:
                counts[i] += 1
            elif sum(counts) > INVOICE_TARGET and counts[i] > 1:
                counts[i] -= 1
        return counts

    def _build_invoices(self) -> None:
        counts = self._assign_counts()
        # One slot per invoice, grouped by counterparty (so per-counterparty order is stable).
        slots: list[Counterparty] = []
        for cp, count in zip(self.counterparties, counts, strict=True):
            slots.extend([cp] * count)

        # Draw buckets from a fixed pool and hand the youngest to reliable payers, the oldest to
        # ghosts/promise-breakers, so the distribution is exact and archetype-appropriate.
        pool: list[int] = []
        for bucket_idx, n in enumerate(_BUCKET_POOL):
            pool.extend([bucket_idx] * n)
        pool.sort()
        ranked = sorted(
            range(len(slots)),
            key=lambda i: _ARCHETYPE_AGE_SCORE[self.cp_archetype[slots[i].id]] + self.rng.random(),
        )
        bucket_for_slot: dict[int, int] = {slot_i: pool[rank] for rank, slot_i in enumerate(ranked)}

        seq = 1000
        for slot_i, cp in enumerate(slots):
            archetype_key = self.cp_archetype[cp.id]
            lo, hi = _BUCKET_DPD[bucket_for_slot[slot_i]]
            dpd = self.rng.randint(lo, hi)
            terms = _terms_days(self.rng)
            due = self.anchor - timedelta(days=dpd)
            seq += 1
            inv = Invoice(
                id=_det_uuid(self.rng),
                merchant_id=self.merchant.id,
                counterparty_id=cp.id,
                invoice_number=f"INV-2026-{seq}",
                amount_paise=0,  # assigned below, correlated with age
                outstanding_paise=0,
                currency="INR",
                issue_date=due - timedelta(days=terms),
                due_date=due,
                terms_days=terms,
                po_ref=f"PO-{self.rng.randint(10000, 99999)}" if self.rng.random() < 0.6 else None,
                has_gst=self.rng.random() < 0.9,
                payment_status=PaymentStatus.UNPAID.value,
                recovery_state=RecoveryState.NOT_STARTED.value,
                inferred_cause=_CAUSE_BY_ARCHETYPE[archetype_key].value,
                days_past_due=dpd,
                aging_bucket=_bucket_of(dpd),
                crosses_msme_45=False,
                touch_count=0,
                current_tone_tier=1,
                created_at=_ist_dt(due - timedelta(days=terms)),
                updated_at=_ist_dt(self.anchor),
            )
            self._apply_recovery_state(inv, archetype_key)
            self.db.add(inv)
            self.invoices.append(inv)
            self.invoices_by_cp.setdefault(cp.id, []).append(inv)

        # Correlate size with age: the largest invoices sit in the oldest buckets ("the big money
        # is stuck"), which lifts the amount-weighted collection period toward the 73-day headline
        # and is exactly the exposure PAYVRA prioritises. Jitter avoids a perfectly sorted result.
        amounts = sorted(_lognormal_paise(self.rng) for _ in self.invoices)
        # alpha blends a uniform random draw with normalised age; it is the correlation knob. The
        # log-normal tail is heavy, so weak jitter barely dents the weighted DSO — this tunes it to
        # land near the 73-day headline. alpha=0 -> uncorrelated (~55d), alpha=1 -> ~97d.
        alpha = _AGE_AMOUNT_ALPHA
        by_age = sorted(
            range(len(self.invoices)),
            key=lambda i: (
                (1 - alpha) * self.rng.random() + alpha * (self.invoices[i].days_past_due / 150.0)
            ),
        )
        for rank, inv_i in enumerate(by_age):
            inv = self.invoices[inv_i]
            inv.amount_paise = amounts[rank]
            inv.outstanding_paise = amounts[rank]

        self.db.flush()
        self._apply_special_invoices()

    def _apply_recovery_state(self, inv: Invoice, archetype_key: str) -> None:
        dpd = inv.days_past_due
        if dpd <= 0:
            inv.recovery_state = (
                RecoveryState.NOT_STARTED.value
                if self.rng.random() < 0.6
                else RecoveryState.NUDGED.value
            )
            inv.current_tone_tier = 1
            return
        if archetype_key == "disputer":
            inv.recovery_state = RecoveryState.HUMAN_REVIEW.value
            inv.current_tone_tier = 1
        elif archetype_key == "wrong_contact":
            inv.recovery_state = RecoveryState.HUMAN_REVIEW.value
            inv.current_tone_tier = 2
        elif archetype_key == "reliable_late":
            inv.recovery_state = (
                RecoveryState.NUDGED.value if dpd <= 30 else RecoveryState.CHASING.value
            )
            inv.current_tone_tier = 1
        elif archetype_key == "ghost":
            inv.recovery_state = RecoveryState.CHASING.value
            inv.current_tone_tier = 3
        else:
            inv.recovery_state = (
                RecoveryState.CHASING.value if dpd <= 75 else RecoveryState.ESCALATED.value
            )
            inv.current_tone_tier = 2 if dpd <= 60 else 3
        inv.touch_count = min(6, max(1, dpd // 20))

    def _apply_special_invoices(self) -> None:
        # 4 settled: from always-pays archetypes.
        settle_pool = [
            inv
            for inv in self.invoices
            if self.cp_archetype[inv.counterparty_id] in ("reliable_late", "chronic_slow")
            and inv.days_past_due > 0
        ]
        for inv in self.rng.sample(settle_pool, 4):
            settled_on = self.anchor - timedelta(days=self.rng.randint(1, 55))
            inv.payment_status = PaymentStatus.PAID.value
            inv.recovery_state = RecoveryState.SETTLED.value
            inv.outstanding_paise = 0
            inv.settled_at = _ist_dt(settled_on, hour=self.rng.randint(9, 18))
            inv.stop_reason = None

        # 6 partial: cash-crunched primarily.
        partial_pool = [
            inv
            for inv in self.invoices
            if self.cp_archetype[inv.counterparty_id] in ("cash_crunched", "chronic_slow")
            and inv.payment_status == PaymentStatus.UNPAID.value
            and inv.days_past_due > 20
        ]
        for inv in self.rng.sample(partial_pool, 6):
            paid = int(inv.amount_paise * self.rng.uniform(0.3, 0.6))
            paid -= paid % 100
            inv.payment_status = PaymentStatus.PARTIALLY_PAID.value
            inv.outstanding_paise = inv.amount_paise - paid
            inv.recovery_state = RecoveryState.PROMISED.value

        # 4 crossing the MSME Act 45-day threshold, on is_msme counterparties.
        msme_pool = [
            inv
            for inv in self.invoices
            if self.cp_by_name_lookup(inv.counterparty_id).is_msme
            and inv.days_past_due > 45
            and inv.payment_status != PaymentStatus.PAID.value
        ]
        for inv in self.rng.sample(msme_pool, 4):
            inv.crosses_msme_45 = True

        self.db.flush()

    def cp_by_name_lookup(self, cp_id: uuid.UUID) -> Counterparty:
        return next(cp for cp in self.counterparties if cp.id == cp_id)

    # -- counterparty roll-ups ----------------------------------------------------------------
    def _roll_up_counterparties(self) -> None:
        for cp in self.counterparties:
            invs = self.invoices_by_cp.get(cp.id, [])
            cp.lifetime_revenue_paise = sum(i.amount_paise for i in invs) + self.rng.randint(
                5_000_000, 50_000_000
            )
            lo, hi = next(a for a in ARCHETYPES if a.key == self.cp_archetype[cp.id]).pay_days
            cp.avg_days_to_pay = Decimal((lo + hi) // 2)
        self.db.flush()

    # -- backdated history -------------------------------------------------------------------
    def _executed_send(
        self, inv: Invoice, days_ago: int, tier: int, opened: bool, clicked: bool
    ) -> Action:
        cp = self.cp_by_name_lookup(inv.counterparty_id)
        contact = self.primary_contact[cp.id]
        sent_on = self.anchor - timedelta(days=days_ago)
        action = Action(
            id=_det_uuid(self.rng),
            merchant_id=self.merchant.id,
            invoice_id=inv.id,
            type=ActionType.SEND_MESSAGE.value,
            status=ActionStatus.EXECUTED.value,
            channel=Channel.EMAIL.value,
            tone_tier=tier,
            proposed_by=ActorType.AGENT.value,
            rationale=f"{inv.days_past_due} days past due; tier {tier} reminder with payment link.",
            gate_verdicts=self._all_pass_verdicts(),
            scheduled_for=_ist_dt(sent_on, hour=9),
            executed_at=_ist_dt(sent_on, hour=9, minute=2),
            created_at=_ist_dt(sent_on, hour=1, minute=30),
        )
        self.db.add(action)
        self.db.flush()
        message = Message(
            id=_det_uuid(self.rng),
            action_id=action.id,
            channel=Channel.EMAIL.value,
            contact_id=contact.id,
            subject=f"Payment reminder: {inv.invoice_number}",
            body="Gentle reminder that this invoice is due. Pay securely via the link below.",
            language=cp.preferred_language,
            tone_tier=tier,
            source="template",
            content_hash=uuid.UUID(int=self.rng.getrandbits(128)).hex[:16],
            validation_passed=True,
            provider_message_id=f"resend_{self.rng.randint(10**6, 10**7)}",
            delivery_status="delivered",
            opened_at=_ist_dt(sent_on, hour=11) if opened else None,
            clicked_at=_ist_dt(sent_on, hour=12) if clicked else None,
            created_at=_ist_dt(sent_on, hour=9, minute=1),
        )
        self.db.add(message)
        self.db.flush()
        action.message_id = message.id
        self._audit(
            action_type="dispatch.send",
            subject_type="action",
            subject_id=action.id,
            outcome="executed",
            rationale=action.rationale,
            gate_verdicts=action.gate_verdicts,
            inputs={"invoice": inv.invoice_number, "channel": "email", "tone_tier": tier},
            at=action.executed_at,
        )
        return action

    def _build_history(self) -> None:
        # A spread of executed sends across in-flight invoices.
        active = [
            inv
            for inv in self.invoices
            if inv.recovery_state in (RecoveryState.CHASING.value, RecoveryState.ESCALATED.value)
        ]
        for inv in self.rng.sample(active, min(10, len(active))):
            touches = self.rng.randint(1, min(3, max(1, inv.touch_count)))
            for _ in range(touches):
                self._executed_send(
                    inv,
                    days_ago=self.rng.randint(3, 55),
                    tier=inv.current_tone_tier,
                    opened=self.rng.random() < 0.6,
                    clicked=self.rng.random() < 0.3,
                )

        # Settle audit entries for the 4 settled invoices (recovered money, outreach stopped).
        for inv in [i for i in self.invoices if i.payment_status == PaymentStatus.PAID.value]:
            assert inv.settled_at is not None
            self._audit(
                action_type="reconcile.settle",
                subject_type="invoice",
                subject_id=inv.id,
                outcome="stopped",
                rationale="Payment confirmed by webhook; invoice settled, pending actions revoked.",
                inputs={"invoice": inv.invoice_number, "amount_paise": inv.amount_paise},
                actor=ActorType.SYSTEM,
                at=inv.settled_at,
            )

        self._build_replies_and_promises()

    def _build_replies_and_promises(self) -> None:
        # 1) Genuine dispute -> freeze + human routing.
        disputer = self.cp_by_name["Pinnacle Infra Projects Pvt Ltd"]
        disp_inv = self.invoices_by_cp[disputer.id][0]
        recv = self.anchor - timedelta(days=self.rng.randint(6, 20))
        self.db.add(
            Reply(
                id=_det_uuid(self.rng),
                invoice_id=disp_inv.id,
                counterparty_id=disputer.id,
                channel=Channel.EMAIL.value,
                raw_text="We are disputing this invoice — the delivered quantity does not match "
                "our PO. Holding payment until reconciled.",
                intent=ReplyIntent.DISPUTE.value,
                confidence=Decimal("0.94"),
                routed_to_human=True,
                received_at=_ist_dt(recv, hour=14),
            )
        )
        disp_inv.recovery_state = RecoveryState.HUMAN_REVIEW.value
        disp_inv.inferred_cause = UnpaidCause.DISPUTE.value

        # 2) The verbatim Hinglish promise-to-pay -> open Promise dated next Tuesday.
        promiser = self.cp_by_name[HINGLISH_REPLY_NAME]
        promise_inv = self.invoices_by_cp[promiser.id][0]
        recv2 = self.anchor - timedelta(days=self.rng.randint(3, 12))
        next_tue = recv2 + timedelta(days=(1 - recv2.weekday()) % 7 + 7)  # the Tuesday after next
        reply2 = Reply(
            id=_det_uuid(self.rng),
            invoice_id=promise_inv.id,
            counterparty_id=promiser.id,
            channel=Channel.WHATSAPP.value,
            raw_text=HINGLISH_REPLY,
            intent=ReplyIntent.PROMISE_TO_PAY.value,
            confidence=Decimal("0.82"),
            extracted_date=next_tue,
            routed_to_human=False,
            received_at=_ist_dt(recv2, hour=16),
        )
        self.db.add(reply2)
        self.db.flush()
        self.db.add(
            Promise(
                id=_det_uuid(self.rng),
                invoice_id=promise_inv.id,
                reply_id=reply2.id,
                promised_date=next_tue,
                promised_amount_paise=None,  # full outstanding
                confidence=Decimal("0.82"),
                status="open",
                created_at=_ist_dt(recv2, hour=16, minute=1),
            )
        )
        promise_inv.recovery_state = RecoveryState.PROMISED.value

        # 3) Wrong-contact reply -> mark the contact stale, switch channel.
        wrong = self.cp_by_name["Sterling Components Pvt Ltd"]
        wrong_inv = self.invoices_by_cp[wrong.id][0]
        recv3 = self.anchor - timedelta(days=self.rng.randint(8, 30))
        self.db.add(
            Reply(
                id=_det_uuid(self.rng),
                invoice_id=wrong_inv.id,
                counterparty_id=wrong.id,
                channel=Channel.EMAIL.value,
                raw_text="You have the wrong person — I left this company. Please stop emailing.",
                intent=ReplyIntent.WRONG_CONTACT.value,
                confidence=Decimal("0.88"),
                routed_to_human=False,
                received_at=_ist_dt(recv3, hour=10),
            )
        )
        self.primary_contact[wrong.id].is_stale = True

        # Two broken promises in the 60-day window (promise-breakers).
        self._broken_promise("Zenith Marketing Pvt Ltd", broken_count=3, stop=True)
        self._broken_promise("Apex Interiors LLP", broken_count=2, stop=False)
        self.db.flush()

    def _broken_promise(self, name: str, broken_count: int, stop: bool) -> None:
        cp = self.cp_by_name[name]
        cp.broken_promise_count = broken_count
        inv = self.invoices_by_cp[cp.id][0]
        made = self.anchor - timedelta(days=self.rng.randint(20, 45))
        promised = made + timedelta(days=self.rng.randint(5, 12))  # already elapsed
        reply = Reply(
            id=_det_uuid(self.rng),
            invoice_id=inv.id,
            counterparty_id=cp.id,
            channel=Channel.SMS.value,
            raw_text="Will clear by end of next week, please hold.",
            intent=ReplyIntent.PROMISE_TO_PAY.value,
            confidence=Decimal("0.79"),
            extracted_date=promised,
            routed_to_human=False,
            received_at=_ist_dt(made, hour=15),
        )
        self.db.add(reply)
        self.db.flush()
        self.db.add(
            Promise(
                id=_det_uuid(self.rng),
                invoice_id=inv.id,
                reply_id=reply.id,
                promised_date=promised,
                promised_amount_paise=None,
                confidence=Decimal("0.79"),
                status="broken",
                resolved_at=_ist_dt(promised + timedelta(days=1), hour=9),
                created_at=_ist_dt(made, hour=15, minute=1),
            )
        )
        if stop:
            inv.recovery_state = RecoveryState.STOPPED.value
            inv.stop_reason = StopReason.BROKEN_PROMISES_EXCEEDED.value
        else:
            inv.recovery_state = RecoveryState.BROKEN_PROMISE.value

    # -- the four seeded blocked audit entries ------------------------------------------------
    def _build_blocked_audit(self) -> None:
        def first_invoice(name: str) -> Invoice:
            return self.invoices_by_cp[self.cp_by_name[name].id][0]

        # 1) time_window — attempted outside 08:00-19:00 IST.
        self._blocked_action(
            inv=first_invoice("Meridian Logistics LLP"),
            failed_check="time_window",
            reason="Attempted 20:14 IST; outside the 08:00-19:00 contact window.",
        )
        # 2) frequency_cap — weekly touch cap already reached.
        self._blocked_action(
            inv=first_invoice("Kaveri Paper Mills Pvt Ltd"),
            failed_check="frequency_cap",
            reason="Weekly touch cap of 2 already reached for this counterparty.",
        )
        # 3) stopping_rules — 3 broken promises.
        self._blocked_action(
            inv=first_invoice("Zenith Marketing Pvt Ltd"),
            failed_check="stopping_rules",
            reason="3 broken promises: counterparty is on the permanent-stop exception list.",
        )
        # 4) freshness — invoice paid between planning and dispatch.
        settled = next(i for i in self.invoices if i.payment_status == PaymentStatus.PAID.value)
        self._blocked_action(
            inv=settled,
            failed_check="freshness",
            reason="Invoice settled after planning and before dispatch; send aborted.",
        )

    def _blocked_action(self, inv: Invoice, failed_check: str, reason: str) -> None:
        when = self.anchor - timedelta(days=self.rng.randint(2, 40))
        verdicts = self._verdicts(failed=failed_check, reason=reason)
        action = Action(
            id=_det_uuid(self.rng),
            merchant_id=self.merchant.id,
            invoice_id=inv.id,
            type=ActionType.SEND_MESSAGE.value,
            status=ActionStatus.GATED_FAIL.value,
            channel=Channel.EMAIL.value,
            tone_tier=inv.current_tone_tier,
            proposed_by=ActorType.AGENT.value,
            rationale=f"Proposed tier {inv.current_tone_tier} reminder for {inv.invoice_number}.",
            gate_verdicts=verdicts,
            gate_failure_reason=reason,
            scheduled_for=_ist_dt(when, hour=20 if failed_check == "time_window" else 10),
            created_at=_ist_dt(when, hour=1, minute=30),
        )
        self.db.add(action)
        self.db.flush()
        self._audit(
            action_type=f"gate.{failed_check}",
            subject_type="action",
            subject_id=action.id,
            outcome="blocked",
            rationale=reason,
            gate_verdicts=verdicts,
            inputs={"invoice": inv.invoice_number, "failed_check": failed_check},
            at=action.created_at,
        )

    # -- metrics -----------------------------------------------------------------------------
    def _build_metrics(self) -> None:
        settled = [i for i in self.invoices if i.payment_status == PaymentStatus.PAID.value]
        open_outstanding = sum(
            i.outstanding_paise
            for i in self.invoices
            if i.payment_status != PaymentStatus.PAID.value
        )
        recovered_total = sum(i.amount_paise for i in settled)

        # api-contracts.md: "Compute it, never assert it." Today's snapshot carries the real
        # amount-weighted collection period, from the same app.metrics formula the seed summary
        # and (Phase 1) GET /metrics use -- so the dashboard cannot contradict the seed on stage.
        # The 14-day run-up is synthetic: reconstructing a true as-of-date DSO needs historical
        # outstanding balances the seed does not model. It is a ramp that LANDS on the computed
        # figure rather than a hardcoded number that happens to look plausible.
        dso_today = collection_period_days(
            (
                (i.issue_date, i.outstanding_paise)
                for i in self.invoices
                if i.payment_status != PaymentStatus.PAID.value
            ),
            as_of=self.anchor,
        )
        dso_start = dso_today + Decimal("7")

        for offset in range(13, -1, -1):
            snap_date = self.anchor - timedelta(days=offset)
            fraction = (13 - offset + 1) / 14
            recovered_so_far = int(recovered_total * fraction)
            self.db.add(
                MetricsSnapshot(
                    id=_det_uuid(self.rng),
                    merchant_id=self.merchant.id,
                    snapshot_date=snap_date,
                    total_outstanding_paise=open_outstanding + recovered_total - recovered_so_far,
                    recovered_paise=int(recovered_total / 14),
                    dso_days=quantize_days(
                        dso_start - (dso_start - dso_today) * Decimal(str(fraction))
                    ),
                    recovery_rate=Decimal(str(round(0.18 * fraction, 4))),
                    promise_kept_rate=Decimal("0.62"),
                    invoices_by_state=self._state_counts(),
                )
            )
        self.db.flush()

    def _state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for inv in self.invoices:
            counts[inv.recovery_state] = counts.get(inv.recovery_state, 0) + 1
        return counts

    # -- helpers -----------------------------------------------------------------------------
    def _all_pass_verdicts(self) -> list[dict[str, object]]:
        return [{"check": c, "passed": True, "reason": "ok"} for c in GATE_CHECKS]

    def _verdicts(self, failed: str, reason: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for c in GATE_CHECKS:
            is_failed = c == failed
            out.append(
                {"check": c, "passed": not is_failed, "reason": reason if is_failed else "ok"}
            )
            if is_failed:  # gate is ordered and halts on first failure
                break
        return out

    def _audit(
        self,
        *,
        action_type: str,
        subject_type: str,
        subject_id: uuid.UUID,
        outcome: str,
        rationale: str,
        inputs: dict[str, object],
        gate_verdicts: list[dict[str, object]] | None = None,
        actor: ActorType = ActorType.AGENT,
        at: datetime | None = None,
    ) -> AuditLog:
        return record(
            self.db,
            merchant_id=self.merchant.id,
            actor=actor,
            action_type=action_type,
            subject_type=subject_type,
            subject_id=subject_id,
            outcome=outcome,
            rationale=rationale,
            inputs=inputs,
            gate_verdicts=gate_verdicts,
            actor_id="seed",
            created_at=at,
        )

    def _write_messy_fixture(self) -> None:
        """Emit the 8 deliberately defective raw rows for the Phase 1 repair queue.

        These are pre-normalisation upload rows that cannot exist as typed Invoice rows (missing
        due date, unparseable amount, ambiguous date). They ship as an ingestion fixture, plus the
        deliberate name variant so the fuzzy matcher is exercised. Not counted in the 120.
        """
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        rows = [
            # header
            ["Invoice #", "Customer", "Amount", "Invoice Date", "Due Date", "GSTIN"],
            # 2 clean rows for context (one uses the deliberate name variant to test fuzzy match)
            ["INV-2026-9001", data.NAME_VARIANT[1], "245000", "05/03/2026", "04/04/2026", ""],
            ["INV-2026-9002", "Krishna Textiles", "88000", "12/02/2026", "13/03/2026", ""],
            # 8 defective rows
            [
                "INV-2026-9003",
                "Deccan Steel Traders Pvt Ltd",
                "156000",
                "18/02/2026",
                "",
                "",
            ],  # missing due date
            [
                "INV-2026-9004",
                "Anand Enterprises",
                "Rs. Twelve Thousand",
                "01/03/2026",
                "31/03/2026",
                "",
            ],  # unparseable amount
            [
                "INV-2026-9005",
                "Meridian Logistics LLP",
                "410000",
                "03/04/2026",
                "03/05/2026",
                "",
            ],  # ambiguous DD/MM vs MM/DD
            ["INV-2026-9006", "", "72000", "20/01/2026", "19/02/2026", ""],  # missing customer
            [
                "INV-2026-9007",
                "Gujarat Polymers Pvt Ltd",
                "-5000",
                "10/02/2026",
                "12/03/2026",
                "",
            ],  # negative amount
            [
                "INV-2026-9008",
                "Surya Pipes & Fittings",
                "199000",
                "32/02/2026",
                "03/03/2026",
                "",
            ],  # impossible date
            [
                "INV-2026-9009",
                "Sri Lakshmi Agencies",
                "134500",
                "15/03/2026",
                "14/02/2026",
                "",
            ],  # due before issue
            [
                "INV-2026-9010",
                "Highland Ceramics",
                "88,000.50",
                "2026/03/01",
                "2026/03/31",
                "BADGSTIN123",
            ],  # bad amount + gstin
        ]
        path = FIXTURES_DIR / "messy_upload.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)


def _summary(db: Session, anchor: date) -> str:
    from sqlalchemy import func, select

    n_inv = db.execute(select(func.count()).select_from(Invoice)).scalar_one()
    n_cp = db.execute(select(func.count()).select_from(Counterparty)).scalar_one()
    n_audit = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()
    n_blocked = db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.outcome == "blocked")
    ).scalar_one()
    buckets: dict[str, int] = {}
    rows = db.execute(
        select(
            Invoice.aging_bucket,
            Invoice.days_past_due,
            Invoice.issue_date,
            Invoice.outstanding_paise,
        ).where(Invoice.payment_status != PaymentStatus.PAID.value)
    ).all()
    for bucket, _dpd, _issue, _out in rows:
        buckets[bucket] = buckets.get(bucket, 0) + 1
    # Both figures, from app.metrics, always labelled. They are different measurements and
    # confusing them once already cost a verification cycle - see agents/data-and-seed.md.
    collection_period = collection_period_days(
        ((issue, out) for _b, _d, issue, out in rows), as_of=anchor
    )
    mean_dpd = mean_days_past_due(dpd for _b, dpd, _issue, _out in rows)
    n_partial = db.execute(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.payment_status == PaymentStatus.PARTIALLY_PAID.value)
    ).scalar_one()
    n_settled = db.execute(
        select(func.count())
        .select_from(Invoice)
        .where(Invoice.payment_status == PaymentStatus.PAID.value)
    ).scalar_one()
    n_msme = db.execute(
        select(func.count()).select_from(Invoice).where(Invoice.crosses_msme_45.is_(True))
    ).scalar_one()
    order = ["current", "0-30", "31-60", "61-90", "90+"]
    total = sum(buckets.values()) or 1
    dist = "  ".join(f"{b}:{buckets.get(b, 0)}({100 * buckets.get(b, 0) // total}%)" for b in order)
    return (
        f"anchor(today IST)={anchor}\n"
        f"counterparties={n_cp}  invoices={n_inv}  audit_entries={n_audit}  blocked={n_blocked}\n"
        f"partial={n_partial}  settled={n_settled}  msme_45_crossings={n_msme}\n"
        f"open aging  {dist}\n"
        f"collection period (amount-weighted, from issue date): {collection_period} d\n"
        f"mean days-past-due (open invoices, from due date):    {mean_dpd} d"
    )


def rebuild_schema() -> None:
    """Drop and recreate the schema via Alembic, so the seed always starts from a clean database.

    The seed cannot TRUNCATE: ``audit_log`` is protected by a BEFORE TRUNCATE trigger (migration
    0002) and that guarantee deliberately has no escape hatch. "Append-only except when a flag is
    set" is not a guarantee. Rebuilding the schema costs a couple of seconds and keeps the
    append-only property intact -- a DROP TABLE during ``downgrade`` is not a TRUNCATE, so the
    trigger is never reached.
    """
    from alembic.config import Config

    from alembic import command

    # Pooled connections would hold locks on tables the downgrade is about to drop.
    engine.dispose()
    cfg = Config(str(ALEMBIC_INI))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine.dispose()


def run(reset: bool = False, demo: bool = False) -> None:
    """Build the seed dataset. ``reset``/``demo`` are accepted for the Make targets; the build is
    always a deterministic full rebuild (schema rebuild + seed), so it is safe to run repeatedly."""
    rebuild_schema()
    db = SessionLocal()
    try:
        builder = SeedBuilder(db)
        builder.build()
        db.commit()
        chain_ok = verify_chain(db, builder.merchant.id)
        mode = "demo" if demo else ("reset" if reset else "seed")
        print(f"[seed:{mode}] done. audit chain verified={chain_ok}")
        print(_summary(db, builder.anchor))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.seed", description="Seed the PAYVRA database.")
    parser.add_argument("--reset", action="store_true", help="rebuild the schema and reseed")
    parser.add_argument("--demo", action="store_true", help="deterministic curated state")
    args = parser.parse_args(argv)
    run(reset=args.reset, demo=args.demo)


if __name__ == "__main__":
    main()
