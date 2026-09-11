"""Authentication events, written to the existing audit_logs table.

What is recorded is who did what to whom, and never anything that could be
replayed. No password, no hash, no reset token, no session token -- not in the
row, not in the metadata, not in a log line. An audit trail that leaks a
credential is worse than no audit trail, because it is a credential store
nobody thinks of as one.

Failures here never fail the operation that prompted them. Being unable to
write an audit row is worth knowing about, but refusing a login because of it
would turn a bookkeeping problem into an outage.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger('auth.audit')

# Actions are a small closed vocabulary so the table can be queried without
# guessing at spellings.
LOGIN_SUCCEEDED = 'AUTH_LOGIN_SUCCEEDED'
LOGIN_FAILED = 'AUTH_LOGIN_FAILED'
LOGOUT = 'AUTH_LOGOUT'
SIGNUP = 'AUTH_SIGNUP'
USER_APPROVED = 'AUTH_USER_APPROVED'
USER_DISABLED = 'AUTH_USER_DISABLED'
ADMIN_GRANTED = 'AUTH_ADMIN_GRANTED'
ADMIN_REVOKED = 'AUTH_ADMIN_REVOKED'
PASSWORD_CHANGED = 'AUTH_PASSWORD_CHANGED'
PASSWORD_RESET_ISSUED = 'AUTH_PASSWORD_RESET_ISSUED'
PASSWORD_RESET_USED = 'AUTH_PASSWORD_RESET_USED'

#: Keys that must never reach the metadata column, whatever a caller passes.
#: Checked rather than trusted: this module is the last point at which a
#: credential can be stopped from being written down permanently.
FORBIDDEN_METADATA = frozenset({
    'password', 'password_hash', 'new_password', 'current_password',
    'token', 'token_hash', 'session_token', 'reset_token', 'secret',
})


def scrub(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Drop anything that looks like a credential, and say that it was dropped.

    Deleting silently would make a future leak invisible in review; the marker
    makes it obvious that a caller tried.
    """
    if not metadata:
        return {}
    clean = {key: value for key, value in metadata.items() if key not in FORBIDDEN_METADATA}
    if len(clean) != len(metadata):
        clean['_redacted'] = sorted(set(metadata) - set(clean))
        logger.error('auth.audit_metadata_redacted', extra={'keys': clean['_redacted']})
    return clean


def record(
    connection_factory,
    action: str,
    *,
    actor_user_id: str | None = None,
    target_user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one audit row.

    actor is who performed the action; target is who it happened to. For a
    self-service action such as a login they are the same, and for an
    administrative one they differ -- which is the question an audit trail
    exists to answer.
    """
    payload = scrub(metadata)
    try:
        with connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs (actor_user_id, action, target_type, target_id, metadata)
                VALUES (%s, %s, 'USER', %s, %s)
                """,
                (
                    actor_user_id,
                    action,
                    target_user_id,
                    json.dumps(payload, ensure_ascii=False) if payload else None,
                ),
            )
    except Exception:  # noqa: BLE001 - bookkeeping must not break the operation
        logger.exception('auth.audit_write_failed', extra={'action': action})
