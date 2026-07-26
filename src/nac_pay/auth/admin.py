"""Admin designation — an env-var allowlist, not a role system.

``ADMIN_EMAILS`` (comma-separated, case-insensitive) names the accounts
allowed to publish shared documents. When auth is off (dev), the default
user is treated as admin so the pages stay testable locally."""

from __future__ import annotations

import os

from .dependencies import auth_required


def admin_emails() -> frozenset[str]:
    raw = os.environ.get("ADMIN_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_admin(user_id: str) -> bool:
    if not auth_required():
        return True
    from nac_pay.storage import UserStore
    user = UserStore().get(user_id)
    if user is None or not user.email:
        return False
    return user.email.lower() in admin_emails()
