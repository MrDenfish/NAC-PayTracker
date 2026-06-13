"""Authentication module — email + password, session cookies, email
verification, password reset. Gated by the ``AUTH_REQUIRED`` env var so
existing tests + dev continue to work with the bundled default user."""

from .dependencies import (
    auth_required,
    clear_session,
    current_user,
    session_secret,
    set_session_user,
)
from .emails import (
    ConsoleEmailSender,
    EmailSender,
    SentEmail,
    get_email_sender,
    reset_email_sender,
    send_password_reset_email,
    send_verification_email,
)
from .middleware import AuthRequiredMiddleware
from .passwords import hash_password, needs_rehash, verify_password
from .tokens import (
    PASSWORD_RESET_TTL_HOURS,
    VERIFICATION_TTL_HOURS,
    TokenLookup,
    consume_email_verification,
    consume_password_reset,
    generate_token,
    issue_email_verification,
    issue_password_reset,
)
from .users import (
    authenticate,
    create_user,
    email_exists,
    find_by_email,
    is_email_verified,
    mark_email_verified,
    update_password,
)

__all__ = [
    "AuthRequiredMiddleware",
    "ConsoleEmailSender",
    "EmailSender",
    "PASSWORD_RESET_TTL_HOURS",
    "SentEmail",
    "TokenLookup",
    "VERIFICATION_TTL_HOURS",
    "auth_required",
    "authenticate",
    "clear_session",
    "consume_email_verification",
    "consume_password_reset",
    "create_user",
    "current_user",
    "email_exists",
    "find_by_email",
    "generate_token",
    "get_email_sender",
    "hash_password",
    "is_email_verified",
    "issue_email_verification",
    "issue_password_reset",
    "mark_email_verified",
    "needs_rehash",
    "reset_email_sender",
    "send_password_reset_email",
    "send_verification_email",
    "session_secret",
    "set_session_user",
    "update_password",
    "verify_password",
]
