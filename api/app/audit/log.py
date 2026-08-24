"""Write and verify the append-only, hash-chained audit log.

Every guardrail verdict, agent decision, and reconciliation event is recorded here. The chain is
**per merchant**: each row's ``prev_hash`` is the previous row's ``entry_hash`` for the same
merchant, and ``entry_hash = sha256(canonical(row) + prev_hash)``.

``record()`` is a read-last-row-then-insert sequence, which races under concurrency. It therefore
takes a Postgres transaction-scoped advisory lock keyed on ``merchant_id`` for the duration of the
enclosing transaction, serialising writers per tenant. The lock is cheap now and expensive to
retrofit once every module writes through here. The caller owns the transaction and must commit.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.enums import ActorType
from app.exceptions import AuditChainError
from app.models.audit_log import AuditLog

GENESIS_HASH = "0" * 64
_LOCK_NAMESPACE = b"payvra.audit_log"


def _advisory_key(merchant_id: uuid.UUID) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock, namespaced to the audit log."""
    digest = hashlib.blake2b(_LOCK_NAMESPACE + merchant_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _canonical(
    *,
    merchant_id: uuid.UUID,
    actor: str,
    actor_id: str | None,
    action_type: str,
    subject_type: str,
    subject_id: uuid.UUID,
    inputs: dict[str, Any],
    rationale: str,
    gate_verdicts: list[dict[str, Any]],
    outcome: str,
    created_at_iso: str,
) -> str:
    """Deterministic serialisation of a row's business fields (excludes id and the hashes)."""
    payload = {
        "merchant_id": str(merchant_id),
        "actor": actor,
        "actor_id": actor_id,
        "action_type": action_type,
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "inputs": inputs,
        "rationale": rationale,
        "gate_verdicts": gate_verdicts,
        "outcome": outcome,
        "created_at": created_at_iso,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _entry_hash(canonical: str, prev_hash: str) -> str:
    return hashlib.sha256((canonical + prev_hash).encode("utf-8")).hexdigest()


def record(
    db: Session,
    *,
    merchant_id: uuid.UUID,
    actor: ActorType | str,
    action_type: str,
    subject_type: str,
    subject_id: uuid.UUID,
    outcome: str,
    rationale: str,
    inputs: dict[str, Any] | None = None,
    gate_verdicts: list[dict[str, Any]] | None = None,
    actor_id: str | None = None,
    created_at: datetime | None = None,
) -> AuditLog:
    """Append one audit entry, chained to the merchant's previous entry.

    Runs inside the caller's transaction; holds a per-merchant advisory lock until that
    transaction commits, so concurrent writers cannot interleave and break the chain.

    ``created_at`` defaults to now; callers (e.g. the seed) may pass a backdated timestamp — it is
    part of the hashed canonical form, so the chain stays valid either way.
    """
    actor_value = str(actor)
    inputs = inputs or {}
    gate_verdicts = gate_verdicts or []

    # Serialise writers for this merchant for the rest of the transaction.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(merchant_id)})

    prev_hash = db.execute(
        select(AuditLog.entry_hash)
        .where(AuditLog.merchant_id == merchant_id)
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if prev_hash is None:
        prev_hash = GENESIS_HASH

    # Set created_at in Python so it is part of the hashed canonical form and known before insert.
    if created_at is None:
        created_at = now_utc()
    canonical = _canonical(
        merchant_id=merchant_id,
        actor=actor_value,
        actor_id=actor_id,
        action_type=action_type,
        subject_type=subject_type,
        subject_id=subject_id,
        inputs=inputs,
        rationale=rationale,
        gate_verdicts=gate_verdicts,
        outcome=outcome,
        created_at_iso=created_at.astimezone(UTC).isoformat(),
    )
    entry_hash = _entry_hash(canonical, prev_hash)

    entry = AuditLog(
        merchant_id=merchant_id,
        actor=actor_value,
        actor_id=actor_id,
        action_type=action_type,
        subject_type=subject_type,
        subject_id=subject_id,
        inputs=inputs,
        rationale=rationale,
        gate_verdicts=gate_verdicts,
        outcome=outcome,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
        created_at=created_at,
    )
    db.add(entry)
    db.flush()  # assign id and make the row visible to the next record() in this transaction
    return entry


def verify_chain(db: Session, merchant_id: uuid.UUID) -> bool:
    """Recompute the whole chain for a merchant and confirm nothing has been tampered with.

    Backs a future ``GET /audit/verify`` and the "chain verified" indicator in the UI.
    """
    rows = (
        db.execute(
            select(AuditLog).where(AuditLog.merchant_id == merchant_id).order_by(AuditLog.id.asc())
        )
        .scalars()
        .all()
    )
    expected_prev = GENESIS_HASH
    for row in rows:
        if row.prev_hash != expected_prev:
            return False
        canonical = _canonical(
            merchant_id=row.merchant_id,
            actor=row.actor,
            actor_id=row.actor_id,
            action_type=row.action_type,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            inputs=row.inputs,
            rationale=row.rationale,
            gate_verdicts=row.gate_verdicts,
            outcome=row.outcome,
            created_at_iso=row.created_at.astimezone(UTC).isoformat(),
        )
        if _entry_hash(canonical, row.prev_hash) != row.entry_hash:
            return False
        expected_prev = row.entry_hash
    return True


def assert_chain(db: Session, merchant_id: uuid.UUID) -> None:
    """Raise :class:`AuditChainError` if the chain does not verify."""
    if not verify_chain(db, merchant_id):
        raise AuditChainError(f"audit_log chain verification failed for merchant {merchant_id}")
