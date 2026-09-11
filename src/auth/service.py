"""Signup, login, logout. Decides nothing about what a user may then read."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from datetime import datetime

from . import audit
from .config import (
    RATE_LIMIT_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS, RESET_TOKEN_MINUTES, STATUS_ACTIVE,
    STATUS_DISABLED, STATUS_PENDING, LocalAuthConfig,
)
from .passwords import (
    MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH, hash_password, needs_rehash,
    new_session_token, verify_password,
)
from .repository import AuthRepository, LoginIdTaken, normalize_login_id

# Nothing in this module logs a password, a token, or a hash. The only identity
# that ever reaches a log line is a user id, which is already in every other log.
logger = logging.getLogger('auth')


class LocalAuthDisabled(Exception):
    """Local authentication is not switched on in this deployment."""


class SignupDisabled(Exception):
    """Local authentication is on, but not open to new accounts."""


class InvalidCredentials(Exception):
    """Wrong password, unknown account, inactive user, or a locked one.

    Deliberately one exception for all of them. Separate errors would let
    anybody discover which login ids exist by watching which failure they get,
    and a lockout notice would confirm an account exists just as loudly.
    """


class AccountPendingApproval(Exception):
    """The credentials were right, but the account has not been let in yet.

    Reported only after the password has been verified. Saying it any earlier
    would let anyone discover which accounts exist by watching which message
    they get -- this way the only people who learn an account is pending are
    the ones who already knew its password.
    """


class AccountDisabled(Exception):
    """Same reasoning as AccountPendingApproval: reported only after the password matched."""


class RateLimited(Exception):
    """Too many attempts from one place, too quickly."""


class NotAnAdministrator(Exception):
    """The caller may not administer accounts."""


class UserNotFound(Exception):
    """No such account."""


class CannotDisableSelf(Exception):
    """An administrator disabling themselves is almost never what was meant."""


class LastAdministrator(Exception):
    """The change would leave the installation with no administrator at all."""


class InvalidPassword(Exception):
    """The submitted password cannot be used."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class InvalidResetToken(Exception):
    """Unknown, expired, or already used."""


class InvalidSignup(Exception):
    """The submitted details cannot be used."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


#: Recent attempt timestamps per client address, for the two unauthenticated
#: endpoints. In-process and best-effort: it resets on restart and each worker
#: keeps its own view. That is the honest scope of a rate limiter that costs no
#: dependency and no round trip, and it still removes the cheapest attack --
#: a script hammering one host. Anything stronger belongs at the edge.
_attempts: dict[str, deque[float]] = defaultdict(deque)

#: Hard cap on tracked addresses so the dictionary cannot grow without bound
#: when the source addresses are themselves the attack.
_MAX_TRACKED_CLIENTS = 10_000


def _check_rate_limit(client: str | None) -> None:
    if client is None:
        return
    now = time.monotonic()
    window = _attempts[client]
    while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    if not window and len(_attempts) > _MAX_TRACKED_CLIENTS:
        _attempts.clear()
        window = _attempts[client]
    if len(window) >= RATE_LIMIT_ATTEMPTS:
        raise RateLimited()
    window.append(now)


def reset_rate_limits() -> None:
    """Clear the counters. For tests, which must not inherit each other's state."""
    _attempts.clear()


class AuthService:
    def __init__(self, repository: AuthRepository, config: LocalAuthConfig):
        self.repository = repository
        self.config = config

    # -- signup ------------------------------------------------------------

    def signup(
        self, *, login_id: str, name: str, password: str, password_confirm: str,
        client: str | None = None,
    ):
        if not self.config.enabled:
            raise LocalAuthDisabled()
        if not self.config.signup_enabled:
            raise SignupDisabled()
        _check_rate_limit(client)

        login_id = normalize_login_id(login_id)
        if not (3 <= len(login_id) <= 100):
            raise InvalidSignup('login_id', '아이디는 3자 이상 100자 이하여야 합니다.')
        if any(character.isspace() for character in login_id):
            raise InvalidSignup('login_id', '아이디에 공백을 사용할 수 없습니다.')
        if not name.strip():
            raise InvalidSignup('name', '이름을 입력해 주세요.')
        if len(name.strip()) > 100:
            raise InvalidSignup('name', '이름은 100자 이하여야 합니다.')
        if password != password_confirm:
            raise InvalidSignup('password_confirm', '비밀번호가 일치하지 않습니다.')
        if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
            raise InvalidSignup(
                'password',
                f'비밀번호는 {MIN_PASSWORD_LENGTH}자 이상 {MAX_PASSWORD_LENGTH}자 이하여야 합니다.',
            )

        try:
            created = self.repository.create_user(
                login_id=login_id, name=name, password=password,
            )
        except LoginIdTaken:
            # Signup is the one place where "this id is taken" has to be said,
            # because the alternative is a form nobody can complete. Login stays
            # uniform, so this leaks the existence of an id and nothing more.
            raise InvalidSignup('login_id', '이미 사용 중인 아이디입니다.') from None

        # user_id only. No login id, no name, and certainly no password: a log
        # line is the easiest place for a credential to end up somewhere it was
        # never meant to be.
        logger.info('auth.signup_pending', extra={'user_id': created['user_id']})
        self._audit(audit.SIGNUP, actor=created['user_id'], target=created['user_id'])
        return created

    # -- login -------------------------------------------------------------

    def login(
        self, *, login_id: str, password: str, client: str | None = None,
    ) -> tuple[str, str, datetime]:
        """Return (user_id, session token, expiry). The token is never stored."""
        if not self.config.enabled:
            raise LocalAuthDisabled()
        _check_rate_limit(client)

        credential = self.repository.credential(login_id)
        if credential is None:
            # Hash a throwaway password anyway. Returning immediately would make
            # an unknown id measurably faster than a known one, which is the
            # same enumeration the uniform message is there to prevent.
            hash_password(password[:MAX_PASSWORD_LENGTH] or 'x')
            raise InvalidCredentials()

        locked = credential['locked_until'] is not None and (
            credential['locked_until'] > datetime.now(credential['locked_until'].tzinfo)
        )
        if locked or not credential['is_active']:
            raise InvalidCredentials()

        if not verify_password(credential['password_hash'], password):
            self.repository.record_failure(str(credential['user_id']))
            logger.warning('auth.login_failed', extra={'user_id': str(credential['user_id'])})
            self._audit(
                audit.LOGIN_FAILED,
                actor=str(credential['user_id']), target=str(credential['user_id']),
                reason='BAD_PASSWORD',
            )
            raise InvalidCredentials()

        user_id = str(credential['user_id'])

        # Lifecycle is checked only now, after the password matched. Ordering
        # it before the check would turn the approval message into an oracle
        # for which login ids exist.
        if credential['status'] == STATUS_PENDING:
            self.repository.clear_failures(user_id)
            self._audit(audit.LOGIN_FAILED, actor=user_id, target=user_id, reason='PENDING')
            raise AccountPendingApproval()
        if credential['status'] != STATUS_ACTIVE:
            self._audit(audit.LOGIN_FAILED, actor=user_id, target=user_id, reason='DISABLED')
            raise AccountDisabled()

        self.repository.clear_failures(user_id)
        if needs_rehash(credential['password_hash']):
            # The library raised its parameters since this hash was written.
            self.repository.update_password_hash(user_id, hash_password(password))

        token = new_session_token()
        expires_at = self.repository.create_session(user_id, token, self.config.session_days)
        logger.info('auth.login', extra={'user_id': user_id})
        self._audit(audit.LOGIN_SUCCEEDED, actor=user_id, target=user_id)
        return user_id, token, expires_at

    # -- session -----------------------------------------------------------

    def logout(self, token: str | None) -> None:
        """Always succeeds. Logging out of nothing is still being logged out."""
        if not token:
            return
        # Resolved before revoking so the audit row can name who it was. After
        # revoking there is nothing left to look up.
        session = self.repository.resolve_session(token)
        self.repository.revoke_session(token)
        if session is not None:
            user_id = str(session['user_id'])
            self._audit(audit.LOGOUT, actor=user_id, target=user_id)

    def resolve(self, token: str | None):
        if not self.config.enabled or not token:
            return None
        return self.repository.resolve_session(token)

    def profile(self, user_id: str):
        return self.repository.profile(user_id)

    # -- administration ----------------------------------------------------

    def require_admin(self, user_id: str) -> None:
        """Gate for every administrative operation.

        Reads users.is_system_admin, which is a different thing from
        document_permissions.permission = 'ADMIN'. That value grants power over
        one document and is already accepted as read access by
        READ_ACL_PREDICATE; being granted it must not make anyone able to
        approve accounts.
        """
        if not self.repository.is_system_admin(user_id):
            raise NotAnAdministrator()

    def list_users(self, admin_id: str, status: str | None = None):
        self.require_admin(admin_id)
        if status is not None and status not in (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED):
            raise InvalidSignup('status', '알 수 없는 상태입니다.')
        return self.repository.list_users(status)

    def departments(self, admin_id: str):
        self.require_admin(admin_id)
        return self.repository.departments()

    def approve(self, admin_id: str, user_id: str):
        """Let somebody in.

        The only place an account becomes usable, and it is a person deciding.
        Approval grants no document permission of its own -- what the account
        can read comes from document_permissions, which is edited separately
        and on purpose.
        """
        self.require_admin(admin_id)
        updated = self.repository.set_status(user_id, STATUS_ACTIVE)
        if updated is None:
            raise UserNotFound()
        logger.info('auth.user_approved', extra={'user_id': user_id, 'by': admin_id})
        self._audit(audit.USER_APPROVED, actor=admin_id, target=user_id)
        return updated

    def disable(self, admin_id: str, user_id: str):
        self.require_admin(admin_id)
        if admin_id == user_id:
            raise CannotDisableSelf()
        self._require_another_admin_remains(user_id)
        updated = self.repository.set_status(user_id, STATUS_DISABLED)
        if updated is None:
            raise UserNotFound()
        # Their live sessions go too, or disabling only takes effect when the
        # browser next happens to log in.
        self.repository.revoke_all_sessions(user_id)
        logger.info('auth.user_disabled', extra={'user_id': user_id, 'by': admin_id})
        self._audit(audit.USER_DISABLED, actor=admin_id, target=user_id)
        return updated

    def set_admin(self, admin_id: str, user_id: str, granted: bool):
        """Grant or revoke administration.

        Revoking runs the same last-administrator check as disabling: both end
        with one fewer administrator, and the failure mode is identical.
        """
        self.require_admin(admin_id)
        if not granted:
            if admin_id == user_id:
                raise CannotDisableSelf()
            self._require_another_admin_remains(user_id)
        updated = self.repository.set_system_admin(user_id, granted)
        if updated is None:
            raise UserNotFound()
        self._audit(
            audit.ADMIN_GRANTED if granted else audit.ADMIN_REVOKED,
            actor=admin_id, target=user_id,
        )
        return updated

    def _require_another_admin_remains(self, user_id: str) -> None:
        """Refuse a change that would leave the installation with no administrator.

        An installation with none cannot approve anyone, cannot restore
        administration to anyone, and has to be repaired from the machine. That
        is a bad thing to discover afterwards, and the check costs one query.
        """
        if not self.repository.is_system_admin(user_id):
            return
        if self.repository.count_active_admins(excluding=user_id) == 0:
            raise LastAdministrator()

    # -- audit -------------------------------------------------------------

    def _audit(self, action: str, *, actor: str | None, target: str | None, **metadata) -> None:
        audit.record(
            self.repository.connection_factory, action,
            actor_user_id=actor, target_user_id=target, metadata=metadata or None,
        )

    # -- passwords ---------------------------------------------------------

    def change_password(
        self, user_id: str, *, current_password: str, new_password: str,
        new_password_confirm: str, keep_token: str | None = None,
    ) -> int:
        """Change one's own password, after proving the current one.

        The current password is required even though the caller is already
        authenticated: an unattended browser is a session, not a person, and
        this is the one operation that would let whoever found it lock the
        owner out.

        Returns how many other sessions were ended.
        """
        if not self.config.enabled:
            raise LocalAuthDisabled()
        self._validate_new_password(new_password, new_password_confirm)

        stored = self.repository.password_hash(user_id)
        if stored is None or not verify_password(stored, current_password):
            # The same error as a failed login, and for the same reason: this
            # says nothing about the account beyond "that was not it".
            raise InvalidCredentials()

        self.repository.set_password(user_id, new_password)
        # Changing a password is how somebody responds to thinking it was
        # known. Leaving the other sessions alive would defeat the point.
        revoked = self.repository.revoke_other_sessions(user_id, keep_token)
        logger.info('auth.password_changed', extra={'user_id': user_id, 'revoked': revoked})
        self._audit(
            audit.PASSWORD_CHANGED, actor=user_id, target=user_id,
            revoked_sessions=revoked,
        )
        return revoked

    def issue_reset(self, admin_id: str, login_id: str) -> str:
        """Produce a one-time token that lets somebody set their own password.

        Deliberately not "set a temporary password". An administrator who sets
        one knows it, and the account is then only as private as their memory
        and whatever channel they sent it over. A token gets a person back in
        without anyone else ever learning what they choose.

        The token is returned once and stored only as a hash.
        """
        self.require_admin(admin_id)
        token = new_session_token()
        user_id = self.repository.issue_reset_token(login_id, token, RESET_TOKEN_MINUTES)
        if user_id is None:
            raise UserNotFound()
        logger.info('auth.reset_issued', extra={'user_id': user_id, 'by': admin_id})
        self._audit(audit.PASSWORD_RESET_ISSUED, actor=admin_id, target=user_id)
        return token

    def complete_reset(self, token: str, new_password: str, new_password_confirm: str) -> None:
        if not self.config.enabled:
            raise LocalAuthDisabled()
        self._validate_new_password(new_password, new_password_confirm)
        user_id = self.repository.user_for_reset_token(token)
        if user_id is None:
            raise InvalidResetToken()
        self.repository.set_password(user_id, new_password)
        # Every session, including any the token's holder may have opened.
        self.repository.revoke_all_sessions(user_id)
        logger.info('auth.reset_used', extra={'user_id': user_id})
        self._audit(audit.PASSWORD_RESET_USED, actor=user_id, target=user_id)

    @staticmethod
    def _validate_new_password(password: str, confirm: str) -> None:
        if password != confirm:
            raise InvalidPassword('비밀번호가 일치하지 않습니다.')
        if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
            raise InvalidPassword(
                f'비밀번호는 {MIN_PASSWORD_LENGTH}자 이상 {MAX_PASSWORD_LENGTH}자 이하여야 합니다.'
            )
