"""Settings for local authentication.

This is the operational way into the system: there is no SSO, OIDC, SAML, LDAP
or AD to defer to. It still ships switched off, because an authentication path
should be something a deployment turned on rather than something it inherited.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: Values that mean yes. Everything else -- unset, empty, 'no', a typo -- means
#: no, because the failure mode of a permissive parser here is an
#: authentication path being open in a deployment nobody meant to open it in.
_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})

#: Name of the browser cookie carrying the session token.
SESSION_COOKIE = 'docsearch_session'

#: How long a session lasts. Long enough not to interrupt a day of testing,
#: short enough that an abandoned browser stops working within the week.
DEFAULT_SESSION_DAYS = 7

#: How long an administrator-issued password reset token stays usable. Short:
#: it is handed over in person or over chat and used immediately, and a token
#: that outlives that conversation is just a spare key lying around.
RESET_TOKEN_MINUTES = 30

#: Failed attempts allowed before a short lockout, and how long it lasts.
#: Deliberately modest: this is a speed bump against online guessing, not an
#: account-security system, and a long lockout on a shared dev box mostly locks
#: out the person testing.
MAX_FAILED_ATTEMPTS = 10
LOCKOUT_SECONDS = 60

#: Per-client-address throttling for the two unauthenticated endpoints. The
#: per-account lockout above cannot see an attacker spreading guesses across
#: many accounts, and this cannot see one distributed across many addresses;
#: together they cover the cheap version of both attacks. Neither is a
#: substitute for a real edge rate limiter, and neither pretends to be.
RATE_LIMIT_ATTEMPTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60

#: Account lifecycle, matching the users.status CHECK.
STATUS_PENDING = 'PENDING'
STATUS_ACTIVE = 'ACTIVE'
STATUS_DISABLED = 'DISABLED'


def _flag(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in _TRUE_VALUES


@dataclass(frozen=True)
class LocalAuthConfig:
    enabled: bool
    signup_enabled: bool
    session_days: int
    cookie_secure: bool

    @classmethod
    def from_env(cls, *, production: bool) -> 'LocalAuthConfig':
        """Resolve the settings.

        Production is *not* forced closed here. There is no other identity
        provider to fall back to, so refusing local auth in production would
        leave the deployment with no way for anyone to log in.

        What stays true is that nothing turns it on implicitly: not APP_ENV,
        not the presence of a database, not a credential sitting in the
        environment. An operator sets LOCAL_AUTH_ENABLED, or nobody logs in.

        The debug identity header is a separate matter and remains refused in
        production -- see api.dependencies.require_user.
        """
        enabled = _flag('LOCAL_AUTH_ENABLED')
        return cls(
            enabled=enabled,
            # Signup is a second switch, not a consequence of the first. A
            # deployment can be handed a fixed set of accounts without also
            # being open to new ones -- and even when it is open, signing up
            # only creates a PENDING account that an administrator must let in.
            signup_enabled=enabled and _flag('SELF_SIGNUP_ENABLED'),
            session_days=int(os.environ.get('LOCAL_AUTH_SESSION_DAYS', DEFAULT_SESSION_DAYS)),
            # HTTP in development, HTTPS in production. Never guessed from the
            # request, which a client controls.
            cookie_secure=_flag('AUTH_COOKIE_SECURE'),
        )
