"""Server-side account administration.

    python -m auth list-users [--status PENDING]
    python -m auth approve --login-id <id>
    python -m auth disable --login-id <id>
    python -m auth grant-admin --login-id <id>
    python -m auth revoke-admin --login-id <id>
    python -m auth reset-password --login-id <id>
    python -m auth grant-user-read --login-id <id> --all-current-documents
    python -m auth revoke-user-read --login-id <id>
    python -m auth grant-public-read --all-current-documents

Exists for the things that cannot come from the web:

  * the first administrator. Nothing over HTTP can create one, because a public
    signup form that mints an administrator is a way to take over the system.
    Somebody with access to the machine makes the first one, and that
    administrator makes any others from the UI.
  * recovery, when there is no working administrator account left.
  * granting read access to documents, which the administration UI does not
    cover. Newly ingested documents get the deployment's default access on
    their own; these commands are for changing what already exists.

Prints names and login ids, which an administrator needs in order to identify
people. Never prints a password or a password hash. `reset-password` prints a
one-time token, which is the one secret this tool emits and is the point of it.
"""
from __future__ import annotations

import argparse
import sys

import psycopg

from ingestion.config import database_url

from . import audit
from .config import RESET_TOKEN_MINUTES, STATUS_ACTIVE, STATUS_DISABLED
from .passwords import new_session_token
from .repository import AuthRepository


def connection_factory():
    dsn = database_url()

    def factory():
        return psycopg.connect(dsn)

    return factory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='auth', description=__doc__.splitlines()[0])
    parser.add_argument(
        'command',
        choices=[
            'list-users', 'approve', 'disable',
            'grant-admin', 'revoke-admin', 'reset-password',
            'grant-user-read', 'revoke-user-read', 'grant-public-read',
        ],
    )
    parser.add_argument('--login-id')
    parser.add_argument('--status')
    parser.add_argument(
        '--all-current-documents', action='store_true',
        help='grant READ on every document currently visible to the system',
    )
    args = parser.parse_args(argv)

    factory = connection_factory()
    repository = AuthRepository(factory)

    if args.command == 'list-users':
        rows = repository.list_users(args.status)
        if not rows:
            print('(none)')
            return 0
        for row in rows:
            admin = ' [admin]' if row['is_system_admin'] else ''
            print(
                f"{row['status']:<9} {str(row['login_id'] or '-'):<20} "
                f"{str(row['name'] or '-'):<16}{admin}"
            )
        return 0

    if args.command == 'grant-public-read':
        return _grant_public_read(args)

    if not args.login_id:
        print('error: --login-id is required', file=sys.stderr)
        return 2

    credential = repository.credential(args.login_id)
    if credential is None:
        print('error: no such account', file=sys.stderr)
        return 2
    user_id = str(credential['user_id'])

    if args.command == 'approve':
        repository.set_status(user_id, STATUS_ACTIVE)
        audit.record(factory, audit.USER_APPROVED, target_user_id=user_id,
                     metadata={'via': 'cli'})
        print(f'approved: {args.login_id}')
        print('  note: approval grants no document permission. '
              'Use grant-user-read to give this account access.')
        return 0

    if args.command == 'disable':
        if _would_remove_last_admin(repository, user_id):
            print('error: refusing to disable the last administrator', file=sys.stderr)
            return 2
        repository.set_status(user_id, STATUS_DISABLED)
        revoked = repository.revoke_all_sessions(user_id)
        audit.record(factory, audit.USER_DISABLED, target_user_id=user_id,
                     metadata={'via': 'cli', 'revoked_sessions': revoked})
        print(f'disabled: {args.login_id} ({revoked} session(s) revoked)')
        return 0

    if args.command in ('grant-admin', 'revoke-admin'):
        granted = args.command == 'grant-admin'
        if not granted and _would_remove_last_admin(repository, user_id):
            print("error: refusing to revoke the last administrator's rights", file=sys.stderr)
            return 2
        repository.grant_system_admin(args.login_id, granted=granted)
        audit.record(
            factory, audit.ADMIN_GRANTED if granted else audit.ADMIN_REVOKED,
            target_user_id=user_id, metadata={'via': 'cli'},
        )
        print(f'{args.command}: {args.login_id}')
        return 0

    if args.command == 'reset-password':
        return _reset_password(repository, factory, args.login_id, user_id)

    if args.command == 'grant-user-read':
        return _grant_user_read(args, user_id)

    return _revoke_user_read(user_id)


def _would_remove_last_admin(repository: AuthRepository, user_id: str) -> bool:
    return (
        repository.is_system_admin(user_id)
        and repository.count_active_admins(excluding=user_id) == 0
    )


def _reset_password(repository, factory, login_id: str, user_id: str) -> int:
    """Issue a one-time token instead of setting a password.

    An administrator who sets a password knows it, and the account is then only
    as private as their memory and whatever channel they used. A token gets the
    person back in without anyone else ever learning what they choose.
    """
    token = new_session_token()
    if repository.issue_reset_token(login_id, token, RESET_TOKEN_MINUTES) is None:
        print('error: no such account', file=sys.stderr)
        return 2
    audit.record(factory, audit.PASSWORD_RESET_ISSUED, target_user_id=user_id,
                 metadata={'via': 'cli'})
    print(f'reset token for {login_id} (valid {RESET_TOKEN_MINUTES} minutes):')
    print()
    print(f'  {token}')
    print()
    print('Give this to the person. They set their own password at /reset-password;')
    print('nobody else ever learns what it is. Using it ends all their sessions.')
    return 0


def _grant_user_read(args, user_id: str) -> int:
    """Give one account READ on every document the system currently holds.

    An ordinary document_permissions row per document, with user_id set. Not an
    ACL change and not a bypass: READ_ACL_PREDICATE has always matched a
    caller's own user_id, and this writes exactly the rows it looks for.

    Explicit --all-current-documents because that is genuinely what it does.
    Soft-deleted documents are skipped, and re-running adds nothing: a document
    ingested later is not covered until this is run again, which keeps the
    grant a decision rather than a standing rule.
    """
    if not args.all_current_documents:
        print('error: --all-current-documents is required (it is what this does)',
              file=sys.stderr)
        return 2

    with psycopg.connect(database_url(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_permissions (document_id, user_id, permission)
            SELECT d.id, %(user_id)s, 'READ'
            FROM documents d
            WHERE d.is_deleted = FALSE
              AND NOT EXISTS (
                  SELECT 1 FROM document_permissions p
                  WHERE p.document_id = d.id AND p.user_id = %(user_id)s
              )
            """,
            {'user_id': user_id},
        )
        granted = cur.rowcount
        conn.commit()

    print(f'granted READ on {granted} document(s) to {args.login_id}')
    return 0


def _grant_public_read(args) -> int:
    """Make every currently registered document readable by approved accounts.

    A backfill. New documents get this at ingest time when
    DOCUMENT_DEFAULT_ACCESS says so; this is for the ones that were already
    there when the policy changed.

    One row per document, with the public principal -- not one row per user per
    document. Removing a document from the policy later is deleting that single
    row, after which only its specific grants apply.

    Still not default allow: a document with no row is readable by nobody, and
    this writes rows.
    """
    if not args.all_current_documents:
        print('error: --all-current-documents is required (it is what this does)',
              file=sys.stderr)
        return 2

    with psycopg.connect(database_url(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO document_permissions (document_id, is_public, permission)
            SELECT d.id, TRUE, 'READ'
            FROM documents d
            WHERE d.is_deleted = FALSE
            ON CONFLICT DO NOTHING
            """
        )
        granted = cur.rowcount
        conn.commit()

    print(f'granted READ on {granted} document(s) to every approved account')
    return 0


def _revoke_user_read(user_id: str) -> int:
    with psycopg.connect(database_url(), autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM document_permissions WHERE user_id = %s AND permission = 'READ'",
            (user_id,),
        )
        removed = cur.rowcount
        conn.commit()
    print(f'revoked {removed} READ grant(s)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
