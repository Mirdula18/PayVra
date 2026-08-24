"""Domain exceptions. Never raise bare ``Exception``; never ``except:`` without a type."""

from __future__ import annotations


class PayvraError(Exception):
    """Base class for all PAYVRA domain errors."""


class ConfigurationError(PayvraError):
    """Invalid or missing configuration."""


class AuditChainError(PayvraError):
    """The audit_log hash chain failed verification — a tamper or a bug, never ignorable."""
