"""Local authentication request and response shapes (contract v1.3).

Nothing here carries a password hash, a session token, a reset token, or a user
id chosen by the caller. Nor a department: the organisation does not use them,
so no authentication shape mentions one -- a form with no such field cannot ask
for one, and a request model with no such field cannot accept one.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    login_id: str = Field(min_length=3, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    password_confirm: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    login_id: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    #: Required even though the caller is already authenticated. An unattended
    #: browser is a session, not a person, and this is the operation that would
    #: let whoever found it lock the owner out.
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)
    new_password_confirm: str = Field(min_length=8, max_length=256)


class ResetPasswordRequest(BaseModel):
    """Completing an administrator-issued reset.

    The token stands in for the forgotten password, so this route is
    unauthenticated -- somebody who cannot log in is exactly who needs it.
    """

    model_config = ConfigDict(extra='forbid', strict=True)

    token: str = Field(min_length=8, max_length=512)
    new_password: str = Field(min_length=8, max_length=256)
    new_password_confirm: str = Field(min_length=8, max_length=256)


class MeResponse(BaseModel):
    """Who the caller is, and nothing more.

    No email, no login id, no session detail, no permission list, no
    department. A page needs a name to greet somebody with; anything further
    would be this endpoint quietly becoming a user directory.
    """

    user_id: str
    name: str | None = None
    #: Whether this account may administer other accounts. Exposed so the UI
    #: can decide whether to offer the admin page at all -- it is not what
    #: authorises anything, which is checked server-side on every admin route.
    is_system_admin: bool = False


class AuthCapabilityResponse(BaseModel):
    """Whether the login and signup pages have anything to talk to.

    Lets the frontend hide a signup link that would only ever fail, without
    guessing from its own build mode -- the switches live on the server.
    """

    local_auth_enabled: bool
    signup_enabled: bool


class SignupResponse(BaseModel):
    """What signing up produces: an account, not access.

    No session is issued and no user id is returned. The account is waiting for
    a person to approve it, and there is nothing yet for the caller to do with
    an identifier.
    """

    status: str
    message: str


class AdminUserOut(BaseModel):
    """One account, as an administrator sees it.

    No password hash, no session, no token, no department. `login_id` is here
    because it is what an administrator matches against the person who asked to
    be let in.
    """

    user_id: UUID
    login_id: str | None = None
    name: str | None = None
    status: str
    is_system_admin: bool
    created_at: datetime


class SetAdminRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    granted: bool
