"""Password hashing — argon2-cffi (OWASP-preferred over bcrypt).

Argon2 is the modern recommendation: memory-hard, no input length limit
(unlike bcrypt's 72-byte cap), and the default parameters are well-tuned
out of the box.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return True iff the password matches the hash. Returns False on
    *any* argon2 error (mismatch, malformed hash, etc.) — never raises.

    Note: argon2-cffi's ``InvalidHashError`` inherits from ``ValueError``,
    not ``Argon2Error`` — so we catch both explicitly.
    """
    try:
        return _HASHER.verify(stored_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """If argon2 parameters have been tightened since the hash was created,
    re-hash on next successful login."""
    return _HASHER.check_needs_rehash(stored_hash)
