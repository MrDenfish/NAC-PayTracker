"""Unit tests for auth/passwords + auth/tokens + auth/users.

The route flow is covered in test_signup_login.py + test_forgot_reset.py.
"""

from __future__ import annotations

import time

from nac_pay.auth import (
    PASSWORD_RESET_TTL_HOURS,
    authenticate,
    consume_email_verification,
    consume_password_reset,
    create_user,
    email_exists,
    find_by_email,
    hash_password,
    is_email_verified,
    issue_email_verification,
    issue_password_reset,
    mark_email_verified,
    needs_rehash,
    update_password,
    verify_password,
)


# ── Password hashing ─────────────────────────────────────────────────


def test_hash_and_verify():
    h = hash_password("correct horse battery staple")
    assert verify_password(h, "correct horse battery staple")
    assert not verify_password(h, "wrong password")


def test_verify_rejects_garbage_hash_without_raising():
    assert verify_password("not-a-hash", "anything") is False


def test_needs_rehash_returns_bool():
    h = hash_password("test")
    assert needs_rehash(h) in (True, False)


# ── User creation + authentication ────────────────────────────────────


def test_create_user_unique_email():
    uid = create_user("alice@example.com", "long enough password")
    assert uid.startswith("u_")
    assert email_exists("alice@example.com")
    assert email_exists("ALICE@example.com")  # case-insensitive


def test_authenticate_returns_user_id_on_match():
    uid = create_user("bob@example.com", "long enough password")
    assert authenticate("bob@example.com", "long enough password") == uid
    # Case-insensitive email
    assert authenticate("BOB@example.com", "long enough password") == uid


def test_authenticate_returns_none_for_wrong_password():
    create_user("carol@example.com", "long enough password")
    assert authenticate("carol@example.com", "wrong password") is None


def test_authenticate_returns_none_for_missing_user():
    """The dummy-hash fallback prevents email enumeration via timing,
    but the return value is still None."""
    assert authenticate("ghost@example.com", "any password") is None


def test_email_verification_round_trip():
    uid = create_user("dave@example.com", "long enough password")
    assert is_email_verified(uid) is False
    token = issue_email_verification(uid)
    lookup = consume_email_verification(token)
    assert lookup.valid is True
    assert lookup.user_id == uid
    mark_email_verified(uid)
    assert is_email_verified(uid) is True


def test_consumed_verification_token_cant_be_reused():
    uid = create_user("eve@example.com", "long enough password")
    token = issue_email_verification(uid)
    assert consume_email_verification(token).valid is True
    second = consume_email_verification(token)
    assert second.valid is False
    assert second.reason == "already_used"


def test_unknown_verification_token_rejected():
    lookup = consume_email_verification("nonexistent-token")
    assert lookup.valid is False
    assert lookup.reason == "not_found"


# ── Password reset ───────────────────────────────────────────────────


def test_password_reset_round_trip_updates_password():
    uid = create_user("frank@example.com", "original password")
    token = issue_password_reset(uid)
    lookup = consume_password_reset(token)
    assert lookup.valid is True
    update_password(uid, "new password")
    assert authenticate("frank@example.com", "original password") is None
    assert authenticate("frank@example.com", "new password") == uid


def test_password_reset_token_single_use():
    uid = create_user("greg@example.com", "any password")
    token = issue_password_reset(uid)
    assert consume_password_reset(token).valid is True
    assert consume_password_reset(token).valid is False


def test_find_by_email_returns_user_id_or_none():
    uid = create_user("hank@example.com", "any password")
    assert find_by_email("hank@example.com") == uid
    assert find_by_email("nobody@example.com") is None


def test_password_reset_ttl_is_one_hour():
    """Reset tokens are short-lived — this guards against accidentally
    changing the constant to something looser."""
    assert PASSWORD_RESET_TTL_HOURS == 1
