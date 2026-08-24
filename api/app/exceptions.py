"""Domain exceptions. Never raise bare ``Exception``; never ``except:`` without a type."""

from __future__ import annotations


class PayvraError(Exception):
    """Base class for all PAYVRA domain errors."""


class ConfigurationError(PayvraError):
    """Invalid or missing configuration."""


class AuditChainError(PayvraError):
    """The audit_log hash chain failed verification — a tamper or a bug, never ignorable."""


class IngestionError(PayvraError):
    """An uploaded file could not be read at all (bad type, empty, corrupt).

    Distinct from a *row-level* validation failure, which is not an exception: those become
    ``batch_rows`` in the repair queue so the merchant can fix them.
    """


class AuthenticationError(PayvraError):
    """The caller could not be resolved to a merchant. Always fails closed (401)."""


class NotFoundError(PayvraError):
    """A resource does not exist, or does not belong to this merchant.

    Deliberately the same error either way: telling a caller "this exists but is not yours"
    leaks the existence of another tenant's data.
    """


class ValidationError(PayvraError):
    """A request was well-formed but semantically invalid (422)."""


class AmbiguousDateFormatError(PayvraError):
    """The batch's date column cannot be resolved to DD/MM or MM/DD.

    Never guess. backend.md is explicit: silently misparsing dates corrupts every downstream
    calculation, so the whole batch goes to the repair queue and we ask.
    """
