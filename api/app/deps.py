"""Shared FastAPI dependencies: DB session and tenant scoping.

**Tenant scoping is the load-bearing part.** ``merchant_id`` is resolved here, from the
``Authorization`` header, and *never* from a path, query, or body parameter (agents/backend.md
ground rule 2). Every router takes it as a dependency so a handler cannot accidentally trust
caller-supplied identity: there is no code path that reads a merchant id off the request.

The token->merchant lookup is a **Phase 1 placeholder**: the bearer token is the merchant's UUID.
There is no user table, no password, and no signing yet -- real auth (sessions, JWT, per-user
roles) is a later phase. What matters architecturally, and what the tenant-isolation tests pin
down, is the *shape*: identity arrives via ``Authorization``, is resolved against the database,
and scopes every query. Replacing this function with real token verification changes nothing
above it.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.exceptions import AuthenticationError
from app.models.merchant import Merchant

__all__ = ["get_db", "current_merchant_id", "DbSession", "MerchantId"]


def current_merchant_id(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> uuid.UUID:
    """Resolve the caller's merchant from the ``Authorization: Bearer <token>`` header.

    Raises :class:`AuthenticationError` (401 via the app's exception handler) when the header is
    missing, malformed, or names a merchant that does not exist. A token for a deleted merchant
    must fail closed, not fall through to an empty result set.
    """
    if not authorization:
        raise AuthenticationError("missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationError("expected 'Authorization: Bearer <token>'")

    try:
        merchant_id = uuid.UUID(token.strip())
    except ValueError as exc:
        raise AuthenticationError("invalid token") from exc

    exists = db.execute(select(Merchant.id).where(Merchant.id == merchant_id)).scalar_one_or_none()
    if exists is None:
        raise AuthenticationError("invalid token")
    return merchant_id


DbSession = Annotated[Session, Depends(get_db)]
MerchantId = Annotated[uuid.UUID, Depends(current_merchant_id)]
