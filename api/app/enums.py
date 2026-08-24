"""Enum single source of truth.

Enum-typed columns are stored as ``VARCHAR`` + a ``CHECK`` constraint rather than native
PostgreSQL ``ENUM`` types (see ``architecture/data-model.md``). Native enums need a hand-written
``ALTER TYPE`` per new value; ``action_type``/``stop_reason``/``unpaid_cause`` will gain values in
later phases. The ``CHECK`` value lists are generated from these ``StrEnum`` classes via
:data:`ENUM_COLUMNS`, so the schema can never drift from the application. ``test_enum_parity``
enforces that.
"""

from __future__ import annotations

from enum import StrEnum


class PaymentStatus(StrEnum):
    UNPAID = "unpaid"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    WRITTEN_OFF = "written_off"


class RecoveryState(StrEnum):
    NOT_STARTED = "not_started"
    NUDGED = "nudged"
    CHASING = "chasing"
    PROMISED = "promised"
    BROKEN_PROMISE = "broken_promise"
    ESCALATED = "escalated"
    HUMAN_REVIEW = "human_review"
    STOPPED = "stopped"
    SETTLED = "settled"


class UnpaidCause(StrEnum):
    OVERSIGHT = "oversight"
    CASH_CRUNCH = "cash_crunch"
    DISPUTE = "dispute"
    WRONG_CONTACT = "wrong_contact"
    AWAITING_DOCS = "awaiting_docs"
    REFUSAL = "refusal"
    UNKNOWN = "unknown"


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class ActionType(StrEnum):
    CREATE_PAYMENT_LINK = "create_payment_link"
    SEND_MESSAGE = "send_message"
    LOG_PROMISE = "log_promise"
    OFFER_INSTALLMENT = "offer_installment"
    SWITCH_CHANNEL = "switch_channel"
    ESCALATE_TIER = "escalate_tier"
    SNOOZE = "snooze"
    MARK_DISPUTED = "mark_disputed"
    STOP = "stop"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    GATED_PASS = "gated_pass"
    GATED_FAIL = "gated_fail"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTED = "executed"
    FAILED = "failed"
    REVOKED = "revoked"


class ReplyIntent(StrEnum):
    DISPUTE = "dispute"
    PROMISE_TO_PAY = "promise_to_pay"
    QUERY = "query"
    REFUSAL = "refusal"
    WRONG_CONTACT = "wrong_contact"
    ACKNOWLEDGMENT = "acknowledgment"
    UNCLEAR = "unclear"


class StopReason(StrEnum):
    SETTLED = "settled"
    DISPUTED = "disputed"
    OPTED_OUT = "opted_out"
    BROKEN_PROMISES_EXCEEDED = "broken_promises_exceeded"
    TOUCH_CAP_REACHED = "touch_cap_reached"
    NO_CONSENT = "no_consent"
    MERCHANT_EXCLUDED = "merchant_excluded"
    WRITTEN_OFF = "written_off"


class ActorType(StrEnum):
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"
    COUNTERPARTY = "counterparty"


class BatchStatus(StrEnum):
    """Lifecycle of one uploaded file. ``awaiting_repair`` means rows need merchant input."""

    PARSING = "parsing"
    AWAITING_REPAIR = "awaiting_repair"
    COMPLETE = "complete"


class BatchRowStatus(StrEnum):
    """Per-row outcome. ``repaired`` is a row the merchant fixed and we then ingested."""

    OK = "ok"
    REPAIR_NEEDED = "repair_needed"
    REPAIRED = "repaired"
    DISCARDED = "discarded"


class RepairErrorCode(StrEnum):
    """Why a row could not become an Invoice.

    Deliberately NOT a CHECK-constrained column: validation rules gain codes often, and a
    migration per new code is friction with no integrity payoff. ``batch_rows.error_code`` is
    plain TEXT; this enum is the application-side vocabulary and what the UI switches on.
    """

    MISSING_INVOICE_NUMBER = "missing_invoice_number"
    MISSING_COUNTERPARTY = "missing_counterparty"
    MISSING_AMOUNT = "missing_amount"
    UNPARSEABLE_AMOUNT = "unparseable_amount"
    NON_POSITIVE_AMOUNT = "non_positive_amount"
    MISSING_DUE_DATE = "missing_due_date"
    UNPARSEABLE_DATE = "unparseable_date"
    IMPOSSIBLE_DATE = "impossible_date"
    DUE_BEFORE_ISSUE = "due_before_issue"
    INVALID_GSTIN = "invalid_gstin"
    AMBIGUOUS_COUNTERPARTY = "ambiguous_counterparty"
    AMBIGUOUS_DATE_FORMAT = "ambiguous_date_format"
    UNMAPPED_REQUIRED_COLUMN = "unmapped_required_column"


# Registry: (table_name, column_name) -> StrEnum. The single source for CHECK-constraint
# generation (see app.models.base.enum_check) and for test_enum_parity.
ENUM_COLUMNS: dict[tuple[str, str], type[StrEnum]] = {
    ("consents", "channel"): Channel,
    ("invoices", "payment_status"): PaymentStatus,
    ("invoices", "recovery_state"): RecoveryState,
    ("invoices", "inferred_cause"): UnpaidCause,
    ("invoices", "stop_reason"): StopReason,
    ("actions", "type"): ActionType,
    ("actions", "status"): ActionStatus,
    ("actions", "channel"): Channel,
    ("actions", "proposed_by"): ActorType,
    ("messages", "channel"): Channel,
    ("replies", "channel"): Channel,
    ("replies", "intent"): ReplyIntent,
    ("audit_log", "actor"): ActorType,
    ("batches", "status"): BatchStatus,
    ("batch_rows", "status"): BatchRowStatus,
}


def enum_values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    """Return the ``str`` values of a ``StrEnum`` in declaration order."""
    return tuple(member.value for member in enum_cls)


def check_expression(column: str, enum_cls: type[StrEnum]) -> str:
    """Build the SQL ``col IN ('a', 'b', ...)`` predicate for a CHECK constraint."""
    joined = ", ".join(f"'{value}'" for value in enum_values(enum_cls))
    return f"{column} IN ({joined})"
