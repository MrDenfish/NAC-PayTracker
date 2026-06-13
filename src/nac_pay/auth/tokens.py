"""Single-use, time-limited tokens for email verification + password reset.

Tokens are 32 bytes (256 bits) of cryptographic randomness, base32-encoded
for URL safety. Storage rows track expiry and used_at so the same token
can never be redeemed twice.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from nac_pay.storage.db import session_scope
from nac_pay.storage.db_models import EmailVerificationRow, PasswordResetRow

VERIFICATION_TTL_HOURS = 24
PASSWORD_RESET_TTL_HOURS = 1


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expiry(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def generate_token() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


# ── Email verification ────────────────────────────────────────────────


def issue_email_verification(user_id: str) -> str:
    token = generate_token()
    with session_scope() as sess:
        sess.add(
            EmailVerificationRow(
                token=token,
                user_id=user_id,
                expires_at=_expiry(VERIFICATION_TTL_HOURS),
            )
        )
    return token


@dataclass(frozen=True)
class TokenLookup:
    user_id: str | None
    valid: bool
    reason: str = ""


def consume_email_verification(token: str) -> TokenLookup:
    with session_scope() as sess:
        row = sess.execute(
            select(EmailVerificationRow).where(EmailVerificationRow.token == token)
        ).scalar_one_or_none()
        if row is None:
            return TokenLookup(user_id=None, valid=False, reason="not_found")
        if row.used_at is not None:
            return TokenLookup(user_id=row.user_id, valid=False, reason="already_used")
        if row.expires_at < _utcnow_iso():
            return TokenLookup(user_id=row.user_id, valid=False, reason="expired")
        row.used_at = _utcnow_iso()
        return TokenLookup(user_id=row.user_id, valid=True)


# ── Password reset ────────────────────────────────────────────────────


def issue_password_reset(user_id: str) -> str:
    token = generate_token()
    with session_scope() as sess:
        sess.add(
            PasswordResetRow(
                token=token,
                user_id=user_id,
                expires_at=_expiry(PASSWORD_RESET_TTL_HOURS),
            )
        )
    return token


def consume_password_reset(token: str) -> TokenLookup:
    with session_scope() as sess:
        row = sess.execute(
            select(PasswordResetRow).where(PasswordResetRow.token == token)
        ).scalar_one_or_none()
        if row is None:
            return TokenLookup(user_id=None, valid=False, reason="not_found")
        if row.used_at is not None:
            return TokenLookup(user_id=row.user_id, valid=False, reason="already_used")
        if row.expires_at < _utcnow_iso():
            return TokenLookup(user_id=row.user_id, valid=False, reason="expired")
        row.used_at = _utcnow_iso()
        return TokenLookup(user_id=row.user_id, valid=True)
