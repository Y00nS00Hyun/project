"""Password hashing and session tokens.

Argon2id, with the library's current defaults. Choosing parameters by hand here
would mean freezing today's guess about hardware into the code; the library
raises them over time, and the parameters live inside each stored hash, so an
old hash keeps verifying after they change.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

#: Minimum length. Short enough not to obstruct development accounts, long
#: enough that the lockout has something to protect.
MIN_PASSWORD_LENGTH = 8

#: Argon2 reads the whole input, so an unbounded field is a way to make the
#: server do unbounded work with one request.
MAX_PASSWORD_LENGTH = 256

#: Bytes of entropy in a session token. 32 bytes is far past guessing.
TOKEN_BYTES = 32

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Return the encoded Argon2id hash. The plaintext is never returned or logged."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification that never raises for a wrong password.

    A malformed stored hash is treated as a failed verification rather than an
    error: it cannot authenticate anyone, and turning it into a 500 would tell
    a caller that this particular account is different from the others.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def new_session_token() -> str:
    """A fresh token for the browser. Exists in plaintext only in the response."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """What the database stores instead of the token.

    A plain SHA-256 rather than a password hash: the token is 256 bits of
    randomness, so there is nothing to brute-force and no reason to make every
    request pay for a memory-hard KDF. The property that matters is that
    reading the table does not hand anyone a usable session.
    """
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)
