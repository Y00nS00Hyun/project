"""Local authentication: credentials, approval, sessions, administration.

This is the operational way into the system, so the tests are about the
properties that keep it one: a password is never stored in a form anyone can
read, a failed login never says which half was wrong, signing up grants
nothing, and an account nobody approved cannot get in.
"""

from __future__ import annotations

import psycopg
import pytest

from auth.config import LocalAuthConfig, SESSION_COOKIE, STATUS_ACTIVE, STATUS_PENDING
from auth.passwords import hash_password, token_fingerprint, verify_password
from auth.repository import AuthRepository, LoginIdTaken, normalize_login_id
from auth.service import (
    AccountDisabled, AccountPendingApproval, AuthService, CannotDisableSelf,
    InvalidCredentials, InvalidPassword, InvalidResetToken, InvalidSignup,
    LastAdministrator, LocalAuthDisabled, NotAnAdministrator, RateLimited,
    SignupDisabled, reset_rate_limits,
)

from support import pgtest
from test_search_backend import (  # noqa: F401
    Corpus, DeterministicEmbedder, conn, connection_factory, corpus, dsn, embedder,
    search_db, server,
)

#: Never a realistic-looking secret, and never a value that also appears in a
#: config file. Tests own these strings; nothing else does.
PASSWORD = 'test-password-0001'
OTHER_PASSWORD = 'test-password-0002'


@pytest.fixture(autouse=True)
def clean_rate_limits():
    # The limiter is process-global, so one test's attempts would otherwise
    # count against the next one's.
    reset_rate_limits()
    yield
    reset_rate_limits()


@pytest.fixture
def config():
    return LocalAuthConfig(
        enabled=True, signup_enabled=True, session_days=7, cookie_secure=False,
    )


@pytest.fixture
def repository(connection_factory):  # noqa: F811
    return AuthRepository(connection_factory)


@pytest.fixture
def service(repository, config):
    return AuthService(repository, config)


def signup(service, login_id='tester', name='테스터', password=PASSWORD, confirm=None):
    return service.signup(
        login_id=login_id, name=name, password=password,
        password_confirm=password if confirm is None else confirm,
    )


def approved(service, repository, login_id='tester'):
    created = signup(service, login_id=login_id)
    repository.set_status(created['user_id'], STATUS_ACTIVE)
    return created


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------

def test_signup_creates_a_pending_account(service, conn):  # noqa: F811
    created = signup(service)
    assert created['status'] == STATUS_PENDING
    with conn.cursor() as cur:
        cur.execute('SELECT status, is_system_admin FROM users WHERE id = %s',
                    (created['user_id'],))
        assert cur.fetchone() == (STATUS_PENDING, False)


def test_signup_grants_no_document_permission(service, conn):  # noqa: F811
    created = signup(service)
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM document_permissions WHERE user_id = %s',
                    (created['user_id'],))
        assert cur.fetchone()[0] == 0


def test_signup_takes_four_fields_and_no_others(service, conn):  # noqa: F811
    """No department, no role, no permission -- the shape has nowhere to put one.

    Asserted against the request model rather than by trying to send something,
    because 'extra=forbid' is what makes the attempt impossible.
    """
    from api.schemas.auth import SignupRequest

    assert set(SignupRequest.model_fields) == {
        'login_id', 'name', 'password', 'password_confirm',
    }


def test_a_new_account_has_no_department(service, conn):  # noqa: F811
    """The organisation does not use departments, so authentication sets none.

    The column stays nullable and the department ACL principal stays supported
    for the documents that still carry one -- nothing in this flow touches
    either.
    """
    created = signup(service)
    with conn.cursor() as cur:
        cur.execute('SELECT department_id FROM users WHERE id = %s', (created['user_id'],))
        assert cur.fetchone()[0] is None


def test_signup_cannot_make_an_administrator(service, conn):  # noqa: F811
    created = signup(service)
    with conn.cursor() as cur:
        cur.execute('SELECT is_system_admin FROM users WHERE id = %s', (created['user_id'],))
        assert cur.fetchone()[0] is False


def test_duplicate_login_id_is_refused(service):
    signup(service)
    with pytest.raises(InvalidSignup) as caught:
        signup(service)
    assert caught.value.field == 'login_id'


def test_login_id_is_case_folded(service):
    signup(service, login_id='Tester')
    # 'Tester' and 'tester' must not become two accounts, or one person can
    # register a near-copy of another's id.
    with pytest.raises(InvalidSignup):
        signup(service, login_id='TESTER')
    assert normalize_login_id('  TeStEr ') == 'tester'


@pytest.mark.parametrize('kwargs, field', [
    ({'login_id': 'ab'}, 'login_id'),
    ({'login_id': 'has space'}, 'login_id'),
    ({'name': '   '}, 'name'),
    ({'password': 'short'}, 'password'),
    ({'confirm': 'test-password-9999'}, 'password_confirm'),
])
def test_invalid_signups_are_refused(service, kwargs, field):
    with pytest.raises(InvalidSignup) as caught:
        signup(service, **kwargs)
    assert caught.value.field == field


def test_signup_is_refused_when_the_switch_is_off(repository, config):
    from dataclasses import replace

    service = AuthService(repository, replace(config, signup_enabled=False))
    with pytest.raises(SignupDisabled):
        signup(service)


def test_nothing_works_when_local_auth_is_off(repository, config):
    from dataclasses import replace

    service = AuthService(repository, replace(config, enabled=False))
    with pytest.raises(LocalAuthDisabled):
        signup(service)
    with pytest.raises(LocalAuthDisabled):
        service.login(login_id='tester', password=PASSWORD)
    # A live cookie stops resolving too, so switching the feature off ends
    # existing sessions rather than only blocking new ones.
    assert service.resolve('anything') is None


# ---------------------------------------------------------------------------
# Passwords are never stored in a readable form
# ---------------------------------------------------------------------------

def test_the_password_is_stored_only_as_an_argon2id_hash(service, conn):  # noqa: F811
    created = signup(service)
    with conn.cursor() as cur:
        cur.execute('SELECT password_hash FROM local_auth_credentials WHERE user_id = %s',
                    (created['user_id'],))
        stored = cur.fetchone()[0]
    assert stored.startswith('$argon2id$')
    assert PASSWORD not in stored
    assert verify_password(stored, PASSWORD)


def test_the_plaintext_appears_nowhere_in_the_database(service, conn):  # noqa: F811
    """Scans every text column of every table, not just the ones we expect.

    A password that leaks into an audit row or an error message is exactly the
    kind of thing a targeted assertion does not catch.
    """
    signup(service)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND data_type IN ('text', 'character varying')
            """
        )
        columns = cur.fetchall()
        for table, column in columns:
            cur.execute(
                f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE %s',
                (f'%{PASSWORD}%',),
            )
            assert cur.fetchone()[0] == 0, f'{table}.{column} contains the plaintext'


def test_two_identical_passwords_hash_differently(service):
    # Per-password salt. Equal hashes would let anyone reading the table see
    # which accounts share a password.
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def test_a_pending_account_cannot_log_in(service):
    signup(service)
    with pytest.raises(AccountPendingApproval):
        service.login(login_id='tester', password=PASSWORD)


def test_approval_is_what_lets_somebody_in(service, repository):
    approved(service, repository)
    user_id, token, expires_at = service.login(login_id='tester', password=PASSWORD)
    assert user_id and token and expires_at


def test_a_wrong_password_and_an_unknown_account_fail_identically(service, repository):
    approved(service, repository)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=OTHER_PASSWORD)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='nobody-at-all', password=OTHER_PASSWORD)


def test_the_pending_message_is_not_an_enumeration_oracle(service):
    """Pending is reported only after the password has been verified.

    Otherwise 'this account is pending' would tell anyone who guessed a login
    id that it exists -- without them knowing the password.
    """
    signup(service)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=OTHER_PASSWORD)
    with pytest.raises(AccountPendingApproval):
        service.login(login_id='tester', password=PASSWORD)


def test_a_disabled_account_cannot_log_in(service, repository):
    created = approved(service, repository)
    repository.set_status(created['user_id'], 'DISABLED')
    with pytest.raises(AccountDisabled):
        service.login(login_id='tester', password=PASSWORD)


def test_repeated_failures_lock_the_account_briefly(service, repository):
    from auth.config import MAX_FAILED_ATTEMPTS

    approved(service, repository)
    for _ in range(MAX_FAILED_ATTEMPTS):
        with pytest.raises(InvalidCredentials):
            service.login(login_id='tester', password=OTHER_PASSWORD)
    # Now even the right password is refused, and refused with the same message
    # as everything else.
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=PASSWORD)


def test_a_successful_login_clears_the_failure_count(service, repository, conn):  # noqa: F811
    created = approved(service, repository)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=OTHER_PASSWORD)
    service.login(login_id='tester', password=PASSWORD)
    with conn.cursor() as cur:
        cur.execute('SELECT failed_attempts FROM local_auth_credentials WHERE user_id = %s',
                    (created['user_id'],))
        assert cur.fetchone()[0] == 0


def test_one_client_cannot_spray_guesses_across_many_accounts(service, repository):
    from auth.config import RATE_LIMIT_ATTEMPTS

    approved(service, repository)
    # The per-account lockout cannot see this: every attempt is a different id.
    for index in range(RATE_LIMIT_ATTEMPTS):
        with pytest.raises(InvalidCredentials):
            service.login(login_id=f'guess{index}', password=OTHER_PASSWORD, client='10.0.0.9')
    with pytest.raises(RateLimited):
        service.login(login_id='tester', password=PASSWORD, client='10.0.0.9')
    # Another address is unaffected.
    service.login(login_id='tester', password=PASSWORD, client='10.0.0.10')


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_only_the_hash_of_a_token_is_stored(service, repository, conn):  # noqa: F811
    approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    with conn.cursor() as cur:
        cur.execute('SELECT token_hash FROM auth_sessions')
        stored = [row[0] for row in cur.fetchall()]
    assert token not in stored
    assert token_fingerprint(token) in stored


def test_a_session_resolves_to_the_user(service, repository):
    approved(service, repository)
    user_id, token, _ = service.login(login_id='tester', password=PASSWORD)
    session = service.resolve(token)
    assert str(session['user_id']) == user_id


def test_logout_revokes_the_session_server_side(service, repository):
    approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    service.logout(token)
    # Not merely 'the browser forgot it': the token itself is now useless.
    assert service.resolve(token) is None


def test_an_invalid_or_absent_token_resolves_to_nobody(service):
    assert service.resolve(None) is None
    assert service.resolve('') is None
    assert service.resolve('not-a-real-token') is None


def test_an_expired_session_stops_working(service, repository, conn):  # noqa: F811
    approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    with conn.cursor() as cur:
        cur.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 second'")
    assert service.resolve(token) is None


def test_disabling_an_account_kills_its_live_sessions(service, repository):
    created = approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    assert service.resolve(token) is not None
    repository.set_status(created['user_id'], 'DISABLED')
    # Checked on every request, so access ends now rather than in seven days.
    assert service.resolve(token) is None


def test_logging_out_twice_is_not_an_error(service, repository):
    approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    service.logout(token)
    service.logout(token)
    service.logout(None)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------

@pytest.fixture
def admin(service, repository):
    created = approved(service, repository, login_id='admin1')
    repository.grant_system_admin('admin1')
    return created


def test_an_ordinary_user_may_not_administer(service, repository):
    created = approved(service, repository)
    with pytest.raises(NotAnAdministrator):
        service.list_users(created['user_id'])


def test_a_document_admin_permission_does_not_confer_user_administration(
    service, repository, corpus,  # noqa: F811
):
    """The two ADMINs are different concepts and must not substitute for each other.

    document_permissions.permission = 'ADMIN' is power over one document, and
    READ_ACL_PREDICATE already accepts it as read access. If it also granted
    account administration, anyone given a document would inherit the system.
    """
    created = approved(service, repository)
    document_id, _ = corpus.document('권한 문서', text='본문')
    corpus.grant(document_id, user_id=created['user_id'], permission='ADMIN')

    with pytest.raises(NotAnAdministrator):
        service.list_users(created['user_id'])


def test_an_administrator_sees_the_pending_queue(service, repository, admin):
    signup(service, login_id='waiting')
    pending = service.list_users(admin['user_id'], STATUS_PENDING)
    assert [row['login_id'] for row in pending] == ['waiting']


def test_approval_only_sets_the_status(service, repository, admin, conn):  # noqa: F811
    created = signup(service, login_id='waiting')
    service.approve(admin['user_id'], created['user_id'])
    with conn.cursor() as cur:
        cur.execute('SELECT status, department_id FROM users WHERE id = %s',
                    (created['user_id'],))
        status, department_id = cur.fetchone()
    assert status == STATUS_ACTIVE
    # Approval is a decision about signing in, not about where somebody works
    # and not about what they may read.
    assert department_id is None


def test_approval_grants_no_document_permission(service, admin, conn):  # noqa: F811
    created = signup(service, login_id='waiting')
    service.approve(admin['user_id'], created['user_id'])
    with conn.cursor() as cur:
        cur.execute('SELECT count(*) FROM document_permissions WHERE user_id = %s',
                    (created['user_id'],))
        # What the account can read comes from the existing ACL through its
        # department, not from anything approval writes.
        assert cur.fetchone()[0] == 0


def test_approval_does_not_make_an_administrator(service, admin, conn):  # noqa: F811
    created = signup(service, login_id='waiting')
    service.approve(admin['user_id'], created['user_id'])
    with conn.cursor() as cur:
        cur.execute('SELECT is_system_admin FROM users WHERE id = %s', (created['user_id'],))
        assert cur.fetchone()[0] is False


def test_an_administrator_cannot_disable_themselves(service, admin):
    with pytest.raises(CannotDisableSelf):
        service.disable(admin['user_id'], admin['user_id'])


def test_disabling_ends_access_immediately(service, repository, admin):
    created = approved(service, repository, login_id='victim')
    _, token, _ = service.login(login_id='victim', password=PASSWORD)
    service.disable(admin['user_id'], created['user_id'])
    assert service.resolve(token) is None
    with pytest.raises(AccountDisabled):
        service.login(login_id='victim', password=PASSWORD)


# ---------------------------------------------------------------------------
# Passwords: changing one's own, and administrator-initiated reset
# ---------------------------------------------------------------------------

NEW_PASSWORD = 'test-password-0003'


def test_changing_a_password_requires_the_current_one(service, repository):
    created = approved(service, repository)
    with pytest.raises(InvalidCredentials):
        service.change_password(
            created['user_id'], current_password=OTHER_PASSWORD,
            new_password=NEW_PASSWORD, new_password_confirm=NEW_PASSWORD,
        )
    # An unattended browser is a session, not a person: whoever found it must
    # not be able to lock the owner out.
    service.login(login_id='tester', password=PASSWORD)


def test_a_changed_password_replaces_the_old_one(service, repository):
    created = approved(service, repository)
    service.change_password(
        created['user_id'], current_password=PASSWORD,
        new_password=NEW_PASSWORD, new_password_confirm=NEW_PASSWORD,
    )
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=PASSWORD)
    service.login(login_id='tester', password=NEW_PASSWORD)


def test_changing_a_password_ends_every_other_session(service, repository):
    created = approved(service, repository)
    _, first, _ = service.login(login_id='tester', password=PASSWORD)
    _, second, _ = service.login(login_id='tester', password=PASSWORD)

    revoked = service.change_password(
        created['user_id'], current_password=PASSWORD,
        new_password=NEW_PASSWORD, new_password_confirm=NEW_PASSWORD,
        keep_token=second,
    )
    assert revoked == 1
    # Changing a password is how somebody responds to thinking it was known.
    assert service.resolve(first) is None
    # The browser doing the changing keeps working; logging it out would be a
    # small hostility with no security value.
    assert service.resolve(second) is not None


@pytest.mark.parametrize('new, confirm', [
    ('short', 'short'),
    (NEW_PASSWORD, 'test-password-9999'),
])
def test_an_unusable_new_password_is_refused(service, repository, new, confirm):
    created = approved(service, repository)
    with pytest.raises(InvalidPassword):
        service.change_password(
            created['user_id'], current_password=PASSWORD,
            new_password=new, new_password_confirm=confirm,
        )


def test_an_administrator_issues_a_token_not_a_password(service, repository, admin, conn):  # noqa: F811
    approved(service, repository)
    token = service.issue_reset(admin['user_id'], 'tester')

    with conn.cursor() as cur:
        cur.execute('SELECT reset_token_hash FROM local_auth_credentials '
                    "WHERE login_id = 'tester'")
        stored = cur.fetchone()[0]
    # Stored as a hash, like a session token: reading the table must not hand
    # anyone a way in.
    assert stored is not None and token not in stored


def test_a_reset_lets_the_person_choose_their_own_password(service, repository, admin):
    approved(service, repository)
    token = service.issue_reset(admin['user_id'], 'tester')
    service.complete_reset(token, NEW_PASSWORD, NEW_PASSWORD)

    service.login(login_id='tester', password=NEW_PASSWORD)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='tester', password=PASSWORD)


def test_a_reset_token_works_once(service, repository, admin):
    approved(service, repository)
    token = service.issue_reset(admin['user_id'], 'tester')
    service.complete_reset(token, NEW_PASSWORD, NEW_PASSWORD)
    with pytest.raises(InvalidResetToken):
        service.complete_reset(token, 'test-password-0004', 'test-password-0004')


def test_an_expired_reset_token_is_refused(service, repository, admin, conn):  # noqa: F811
    approved(service, repository)
    token = service.issue_reset(admin['user_id'], 'tester')
    with conn.cursor() as cur:
        cur.execute("UPDATE local_auth_credentials "
                    "SET reset_expires_at = now() - interval '1 second'")
    with pytest.raises(InvalidResetToken):
        service.complete_reset(token, NEW_PASSWORD, NEW_PASSWORD)


def test_a_completed_reset_ends_every_session(service, repository, admin):
    approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    reset = service.issue_reset(admin['user_id'], 'tester')
    service.complete_reset(reset, NEW_PASSWORD, NEW_PASSWORD)
    # Including any the token's holder may have opened.
    assert service.resolve(token) is None


def test_only_an_administrator_can_issue_a_reset(service, repository):
    created = approved(service, repository)
    approved(service, repository, login_id='victim')
    with pytest.raises(NotAnAdministrator):
        service.issue_reset(created['user_id'], 'victim')


def test_an_unknown_token_is_refused(service):
    with pytest.raises(InvalidResetToken):
        service.complete_reset('not-a-real-token', NEW_PASSWORD, NEW_PASSWORD)


# ---------------------------------------------------------------------------
# The last administrator
# ---------------------------------------------------------------------------

def test_the_count_of_administrators_can_never_reach_zero(service, repository, admin):
    """The invariant, stated directly.

    Two guards enforce it, and which one fires depends on the route. Over
    HTTP the actor is themselves an administrator, so removing anyone *else*
    always leaves at least the actor, and the only way to reach zero would be
    removing oneself -- which CannotDisableSelf refuses. The explicit
    last-administrator check is what covers the route with no actor: the CLI.

    Asserting the invariant rather than a particular exception means the test
    keeps its meaning if the guards are ever rearranged.
    """
    second = approved(service, repository, login_id='admin2')
    repository.grant_system_admin('admin2')
    assert repository.count_active_admins() == 2

    # Removing one of two is fine.
    service.disable(second['user_id'], admin['user_id'])
    assert repository.count_active_admins() == 1

    # The one that is left cannot remove themselves, by either route.
    with pytest.raises(CannotDisableSelf):
        service.disable(second['user_id'], second['user_id'])
    with pytest.raises(CannotDisableSelf):
        service.set_admin(second['user_id'], second['user_id'], False)
    assert repository.count_active_admins() == 1

    # Ordinary accounts are unaffected by the protection.
    other = approved(service, repository, login_id='plain')
    service.disable(second['user_id'], other['user_id'])
    assert repository.count_active_admins() == 1


def test_the_last_administrator_is_refused_where_there_is_no_actor(service, repository, admin):
    """The CLI route, which has no acting administrator to fall back on."""
    from auth.cli import _would_remove_last_admin

    assert _would_remove_last_admin(repository, admin['user_id']) is True

    second = approved(service, repository, login_id='admin2')
    repository.grant_system_admin('admin2')
    # With a second one, removing either is safe.
    assert _would_remove_last_admin(repository, admin['user_id']) is False
    assert _would_remove_last_admin(repository, second['user_id']) is False

    # An ordinary account is never the last administrator.
    other = approved(service, repository, login_id='plain')
    assert _would_remove_last_admin(repository, other['user_id']) is False


def test_the_service_guard_refuses_a_change_that_would_empty_the_role(service, repository, admin):
    """Defence in depth, reached directly.

    Unreachable through the admin API today because the self-check fires first,
    and kept anyway: it is the guard that stays correct if that ordering ever
    changes.
    """
    with pytest.raises(LastAdministrator):
        service._require_another_admin_remains(admin['user_id'])

    second = approved(service, repository, login_id='admin2')
    repository.grant_system_admin('admin2')
    service._require_another_admin_remains(admin['user_id'])


def test_an_administrator_can_promote_and_demote_others(service, repository, admin):
    other = approved(service, repository, login_id='plain')
    service.set_admin(admin['user_id'], other['user_id'], True)
    assert repository.is_system_admin(other['user_id']) is True
    service.set_admin(admin['user_id'], other['user_id'], False)
    assert repository.is_system_admin(other['user_id']) is False


def test_promotion_is_not_reachable_without_being_an_administrator(service, repository):
    created = approved(service, repository)
    other = approved(service, repository, login_id='plain')
    with pytest.raises(NotAnAdministrator):
        service.set_admin(created['user_id'], other['user_id'], True)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit_actions(conn):  # noqa: F811
    with conn.cursor() as cur:
        cur.execute('SELECT action FROM audit_logs ORDER BY created_at')
        return [row[0] for row in cur.fetchall()]


def test_every_authentication_event_is_recorded(service, repository, admin, conn):  # noqa: F811
    from auth import audit as audit_module

    created = signup(service, login_id='subject')
    service.approve(admin['user_id'], created['user_id'])
    _, token, _ = service.login(login_id='subject', password=PASSWORD)
    service.logout(token)
    with pytest.raises(InvalidCredentials):
        service.login(login_id='subject', password=OTHER_PASSWORD)
    service.change_password(
        created['user_id'], current_password=PASSWORD,
        new_password=NEW_PASSWORD, new_password_confirm=NEW_PASSWORD,
    )
    reset = service.issue_reset(admin['user_id'], 'subject')
    service.complete_reset(reset, 'test-password-0005', 'test-password-0005')
    service.set_admin(admin['user_id'], created['user_id'], True)
    service.set_admin(admin['user_id'], created['user_id'], False)
    service.disable(admin['user_id'], created['user_id'])

    recorded = set(audit_actions(conn))
    assert {
        audit_module.SIGNUP, audit_module.USER_APPROVED,
        audit_module.LOGIN_SUCCEEDED, audit_module.LOGIN_FAILED, audit_module.LOGOUT,
        audit_module.PASSWORD_CHANGED,
        audit_module.PASSWORD_RESET_ISSUED, audit_module.PASSWORD_RESET_USED,
        audit_module.ADMIN_GRANTED, audit_module.ADMIN_REVOKED,
        audit_module.USER_DISABLED,
    } <= recorded


def test_no_audit_row_can_contain_a_credential(service, repository, admin, conn):  # noqa: F811
    """Scans the whole table, not the rows we expect to be risky.

    An audit trail that leaks a credential is worse than none, because it is a
    credential store nobody thinks of as one.
    """
    created = approved(service, repository)
    _, token, _ = service.login(login_id='tester', password=PASSWORD)
    service.change_password(
        created['user_id'], current_password=PASSWORD,
        new_password=NEW_PASSWORD, new_password_confirm=NEW_PASSWORD,
    )
    reset = service.issue_reset(admin['user_id'], 'tester')

    with conn.cursor() as cur:
        cur.execute('SELECT coalesce(metadata::text, %s) FROM audit_logs', ('',))
        blob = ' '.join(row[0] for row in cur.fetchall())
    for secret in (PASSWORD, NEW_PASSWORD, token, reset):
        assert secret not in blob


def test_forbidden_metadata_is_stripped_rather_than_stored(service, repository, conn):  # noqa: F811
    from auth import audit as audit_module

    created = approved(service, repository)
    audit_module.record(
        repository.connection_factory, audit_module.LOGIN_SUCCEEDED,
        actor_user_id=created['user_id'], target_user_id=created['user_id'],
        metadata={'password': PASSWORD, 'token': 'abc', 'reason': 'ok'},
    )
    with conn.cursor() as cur:
        cur.execute('SELECT metadata FROM audit_logs ORDER BY created_at DESC LIMIT 1')
        metadata = cur.fetchone()[0]
    assert PASSWORD not in str(metadata) and 'abc' not in str(metadata)
    assert metadata['reason'] == 'ok'
    # Dropped loudly, so a future leak is visible in review rather than silent.
    assert set(metadata['_redacted']) == {'password', 'token'}


def test_an_audit_failure_does_not_break_the_operation(service, repository):
    from auth import audit as audit_module

    def broken():
        raise RuntimeError('database unavailable')

    # Being unable to write a bookkeeping row must not turn into a refused
    # login.
    audit_module.record(broken, audit_module.LOGIN_SUCCEEDED, actor_user_id=None)
