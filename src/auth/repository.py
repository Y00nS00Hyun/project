"""Short transactions for credentials and sessions.

Everything a caller can influence is a bind parameter. No SQL in this file is
built by concatenating anything that came from a request.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row

from .config import LOCKOUT_SECONDS, MAX_FAILED_ATTEMPTS, STATUS_ACTIVE, STATUS_PENDING
from .passwords import hash_password, token_fingerprint


class LoginIdTaken(Exception):
    """That login id already belongs to someone."""


class DepartmentMissing(Exception):
    """The configured default department does not exist."""


def normalize_login_id(raw: str) -> str:
    """Fold to one canonical form so 'Alice' and 'alice' are one account."""
    return raw.strip().lower()


class AuthRepository:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    # -- signup ------------------------------------------------------------

    def create_user(self, *, login_id: str, name: str, password: str) -> dict[str, Any]:
        """Create the person and their credential together, or neither.

        The account is created PENDING. Signing up and being let in are
        deliberately not the same act: this is the operational login for an
        internal document system, so anyone who can reach the page could
        otherwise create themselves access to it.

        Two things this method cannot do, by construction rather than by
        checking: make anyone an administrator (the column is never written
        here, so it keeps its FALSE default), and grant any document permission
        (no row is written to document_permissions).

        It does not set a department either. The organisation does not use
        them, so an account has none and nothing in this flow supplies one.
        users.department_id stays nullable and stays NULL; the column and the
        department ACL principal remain for the documents that still use them.
        """
        login_id = normalize_login_id(login_id)
        # Hashed outside the transaction: Argon2id is deliberately slow, and
        # holding a write transaction open for its duration would serialise
        # signups behind each other.
        password_hash = hash_password(password)

        with self.connection_factory() as conn:
            conn.autocommit = False
            try:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(
                        'SELECT 1 FROM local_auth_credentials WHERE login_id = %s',
                        (login_id,),
                    )
                    if cur.fetchone() is not None:
                        raise LoginIdTaken()

                    # sso_subject is NOT NULL UNIQUE and belongs to the identity
                    # provider. Namespacing local accounts keeps them from ever
                    # colliding with a real subject when SSO is wired up.
                    cur.execute(
                        """
                        INSERT INTO users (sso_subject, name, status)
                        VALUES (%s, %s, %s)
                        RETURNING id
                        """,
                        (f'local:{login_id}', name.strip(), STATUS_PENDING),
                    )
                    user_id = str(cur.fetchone()['id'])

                    cur.execute(
                        """
                        INSERT INTO local_auth_credentials (user_id, login_id, password_hash)
                        VALUES (%s, %s, %s)
                        """,
                        (user_id, login_id, password_hash),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {'user_id': user_id, 'login_id': login_id, 'status': STATUS_PENDING}

    # -- login -------------------------------------------------------------

    def credential(self, login_id: str) -> dict[str, Any] | None:
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT c.user_id, c.login_id, c.password_hash, c.failed_attempts,
                       c.locked_until, u.is_active, u.status
                FROM local_auth_credentials c
                JOIN users u ON u.id = c.user_id
                WHERE c.login_id = %s
                """,
                (normalize_login_id(login_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def record_failure(self, user_id: str) -> None:
        """Count a failed attempt and lock the account briefly past the limit."""
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE local_auth_credentials
                SET failed_attempts = failed_attempts + 1,
                    locked_until = CASE
                        WHEN failed_attempts + 1 >= %s
                        THEN now() + make_interval(secs => %s)
                        ELSE locked_until
                    END,
                    updated_at = now()
                WHERE user_id = %s
                """,
                (MAX_FAILED_ATTEMPTS, LOCKOUT_SECONDS, user_id),
            )

    def clear_failures(self, user_id: str) -> None:
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE local_auth_credentials
                SET failed_attempts = 0, locked_until = NULL, updated_at = now()
                WHERE user_id = %s
                """,
                (user_id,),
            )

    def update_password_hash(self, user_id: str, password_hash: str) -> None:
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                'UPDATE local_auth_credentials SET password_hash = %s, updated_at = now() '
                'WHERE user_id = %s',
                (password_hash, user_id),
            )

    # -- sessions ----------------------------------------------------------

    def create_session(self, user_id: str, token: str, days: int) -> datetime:
        expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_sessions (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, token_fingerprint(token), expires_at),
            )
        return expires_at

    def resolve_session(self, token: str) -> dict[str, Any] | None:
        """Return the live session's user, or None.

        Expiry and revocation are part of the WHERE clause rather than checked
        afterwards in Python: a lookup that returns a row the caller then has to
        remember to validate is a lookup somebody will eventually use without
        validating.
        """
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.id AS session_id, u.id AS user_id, u.department_id
                FROM auth_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > now()
                  AND u.is_active = TRUE
                  -- Checked on every request, not only at login: revoking
                  -- somebody's access must take their live session with it,
                  -- not wait a week for it to expire.
                  AND u.status = %s
                """,
                (token_fingerprint(token), STATUS_ACTIVE),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def revoke_session(self, token: str) -> bool:
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                'UPDATE auth_sessions SET revoked_at = now() '
                'WHERE token_hash = %s AND revoked_at IS NULL',
                (token_fingerprint(token),),
            )
            return cur.rowcount == 1

    def profile(self, user_id: str) -> dict[str, Any] | None:
        """The little that /auth/me is allowed to say about someone."""
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT u.id AS user_id, u.name, u.is_system_admin
                FROM users u
                WHERE u.id = %s AND u.is_active = TRUE
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            # Stringified here, at the edge of the database, so that every
            # caller gets the same shape and none has to remember to convert.
            return {**dict(row), 'user_id': str(row['user_id'])}

    # -- administration ----------------------------------------------------

    def revoke_all_sessions(self, user_id: str) -> int:
        """End every live session a user has.

        Called when an account is disabled. resolve_session already refuses a
        non-ACTIVE user, so this is belt and braces -- but leaving rows that
        claim to be live for somebody who is not is the kind of untruth that
        outlives the reason it was tolerated.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                'UPDATE auth_sessions SET revoked_at = now() '
                'WHERE user_id = %s AND revoked_at IS NULL',
                (user_id,),
            )
            return cur.rowcount

    def list_users(self, status: str | None = None) -> list[dict[str, Any]]:
        """Accounts, for the administration screen.

        No password hash and no session data. An administrator needs to decide
        whether to let somebody in, which takes a name, a login id and a status.
        """
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT u.id AS user_id, u.name, u.status, u.is_system_admin,
                       u.created_at, c.login_id
                FROM users u
                LEFT JOIN local_auth_credentials c ON c.user_id = u.id
                WHERE (%s::text IS NULL OR u.status = %s::text)
                ORDER BY
                    -- Pending first: that queue is why the page exists.
                    CASE WHEN u.status = 'PENDING' THEN 0 ELSE 1 END,
                    u.created_at DESC
                """,
                (status, status),
            )
            return [dict(row) for row in cur.fetchall()]

    def set_status(self, user_id: str, status: str) -> dict[str, Any] | None:
        """Approve, or disable. Never grants administration, never a permission.

        Status is the whole of it. Approval is a decision about whether somebody
        may sign in, and deliberately not a decision about what they may read --
        that stays with document_permissions, where it can be seen.
        """
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE users
                SET status = %s, updated_at = now()
                WHERE id = %s
                RETURNING id AS user_id, name, status
                """,
                (status, user_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def count_active_admins(self, excluding: str | None = None) -> int:
        """How many administrators would remain without ``excluding``.

        Used before a change that could remove one. An installation with no
        administrator cannot approve anyone, cannot re-grant administration,
        and has to be repaired from the machine -- so the last one is worth
        refusing to remove.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM users
                WHERE is_system_admin = TRUE
                  AND is_active = TRUE
                  AND status = 'ACTIVE'
                  AND (%s::uuid IS NULL OR id <> %s::uuid)
                """,
                (excluding, excluding),
            )
            return cur.fetchone()[0]

    def is_system_admin(self, user_id: str) -> bool:
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT is_system_admin FROM users "
                "WHERE id = %s AND is_active = TRUE AND status = 'ACTIVE'",
                (user_id,),
            )
            row = cur.fetchone()
            return bool(row and row[0])

    def grant_system_admin(self, login_id: str, granted: bool = True) -> str | None:
        """Make somebody an administrator. Reachable only from the server CLI.

        No HTTP path leads here. The first administrator has to be made by
        somebody with access to the machine, because the alternative -- letting
        the first signup become one -- turns a public form into a way to take
        the system over.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET is_system_admin = %s, updated_at = now()
                WHERE id = (SELECT user_id FROM local_auth_credentials WHERE login_id = %s)
                RETURNING id
                """,
                (granted, normalize_login_id(login_id)),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    # -- passwords ---------------------------------------------------------

    def password_hash(self, user_id: str) -> str | None:
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                'SELECT password_hash FROM local_auth_credentials WHERE user_id = %s',
                (user_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_password(self, user_id: str, password: str) -> bool:
        """Replace the password and clear any outstanding reset.

        Both in one statement: a reset token that survived the password it was
        issued for would be a second, forgotten way into the account.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE local_auth_credentials
                SET password_hash = %s,
                    reset_token_hash = NULL,
                    reset_expires_at = NULL,
                    failed_attempts = 0,
                    locked_until = NULL,
                    updated_at = now()
                WHERE user_id = %s
                """,
                (hash_password(password), user_id),
            )
            return cur.rowcount == 1

    def issue_reset_token(self, login_id: str, token: str, minutes: int) -> str | None:
        """Store the hash of a reset token against an account.

        One outstanding token per account: issuing a new one replaces the old,
        so an administrator who issues twice has not left two ways in.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE local_auth_credentials
                SET reset_token_hash = %s,
                    reset_expires_at = now() + make_interval(mins => %s),
                    updated_at = now()
                WHERE login_id = %s
                RETURNING user_id
                """,
                (token_fingerprint(token), minutes, normalize_login_id(login_id)),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def user_for_reset_token(self, token: str) -> str | None:
        """Whose account this token opens, if it is still live.

        Expiry is in the WHERE clause for the same reason session expiry is: a
        lookup that returns a row the caller must then remember to validate is
        one somebody will eventually use without validating.
        """
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.user_id
                FROM local_auth_credentials c
                JOIN users u ON u.id = c.user_id
                WHERE c.reset_token_hash = %s
                  AND c.reset_expires_at > now()
                  AND u.is_active = TRUE
                  AND u.status = 'ACTIVE'
                """,
                (token_fingerprint(token),),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None

    def revoke_other_sessions(self, user_id: str, keep_token: str | None) -> int:
        """End every session except the one asking.

        Changing a password is how somebody responds to thinking it was known,
        so the other sessions have to go -- but logging the person out of the
        browser they just used to fix it would be its own small hostility.
        """
        keep = token_fingerprint(keep_token) if keep_token else None
        with self.connection_factory() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth_sessions SET revoked_at = now()
                WHERE user_id = %s
                  AND revoked_at IS NULL
                  AND (%s::text IS NULL OR token_hash <> %s::text)
                """,
                (user_id, keep, keep),
            )
            return cur.rowcount

    def set_system_admin(self, user_id: str, granted: bool) -> dict[str, Any] | None:
        with self.connection_factory() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE users SET is_system_admin = %s, updated_at = now()
                WHERE id = %s
                RETURNING id AS user_id, name, status, is_system_admin
                """,
                (granted, user_id),
            )
            row = cur.fetchone()
        return dict(row) if row else None
