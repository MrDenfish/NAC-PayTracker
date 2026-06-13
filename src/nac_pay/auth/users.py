"""User creation / lookup helpers for the auth flow.

Thin wrapper over the ORM that handles the SaaS-account concerns the
``UserStore`` doesn't (password hashing, email lookups, verification
state) so route handlers stay readable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import UserRow

from .passwords import hash_password, verify_password


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _generate_user_id() -> str:
    return "u_" + uuid.uuid4().hex[:24]


# ── Creation ─────────────────────────────────────────────────────────


def create_user(email: str, password: str) -> str:
    """Create a new user pending email verification. Returns the user_id."""
    normalized = _normalize_email(email)
    user_id = _generate_user_id()
    with session_scope() as sess:
        sess.add(
            UserRow(
                user_id=user_id,
                email=normalized,
                created_at=_utcnow_iso(),
                is_default=False,
                password_hash=hash_password(password),
                email_verified_at=None,
            )
        )
    return user_id


def email_exists(email: str) -> bool:
    normalized = _normalize_email(email)
    with session_scope() as sess:
        return sess.execute(
            select(UserRow.user_id).where(UserRow.email == normalized)
        ).scalar_one_or_none() is not None


def find_by_email(email: str) -> str | None:
    """Return user_id for email, or None. Used by /login + /forgot."""
    normalized = _normalize_email(email)
    with session_scope() as sess:
        return sess.execute(
            select(UserRow.user_id).where(UserRow.email == normalized)
        ).scalar_one_or_none()


# ── Authentication ───────────────────────────────────────────────────


_DECOY_HASH = hash_password("decoy-dummy-only-for-timing-equalization")


def authenticate(email: str, password: str) -> str | None:
    """Verify credentials. Returns user_id on success, None on failure.

    When the email is unknown we still run a real verify against a decoy
    hash so the timing matches a successful lookup — defeats email
    enumeration via login response latency."""
    normalized = _normalize_email(email)
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.email == normalized)
        ).scalar_one_or_none()
        if row is None or row.password_hash is None:
            verify_password(_DECOY_HASH, password)
            return None
        if not verify_password(row.password_hash, password):
            return None
        return row.user_id


def is_email_verified(user_id: str) -> bool:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow.email_verified_at).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        return row is not None and row != ""


def mark_email_verified(user_id: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is not None:
            row.email_verified_at = _utcnow_iso()


def update_password(user_id: str, new_password: str) -> None:
    with session_scope() as sess:
        row = sess.execute(
            select(UserRow).where(UserRow.user_id == user_id)
        ).scalar_one_or_none()
        if row is not None:
            row.password_hash = hash_password(new_password)
