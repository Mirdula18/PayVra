"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from __future__ import annotations

from app.models.action import Action
from app.models.audit_log import AuditLog
from app.models.base import Base
from app.models.consent import Consent
from app.models.contact import Contact
from app.models.counterparty import Counterparty
from app.models.invoice import Invoice
from app.models.merchant import Merchant
from app.models.message import Message
from app.models.metrics_snapshot import MetricsSnapshot
from app.models.payment_link import PaymentLink
from app.models.promise import Promise
from app.models.reply import Reply
from app.models.webhook_event import WebhookEvent

__all__ = [
    "Base",
    "Merchant",
    "Counterparty",
    "Contact",
    "Consent",
    "Invoice",
    "PaymentLink",
    "Action",
    "Message",
    "Promise",
    "Reply",
    "WebhookEvent",
    "AuditLog",
    "MetricsSnapshot",
]
