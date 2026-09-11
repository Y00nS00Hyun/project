import { getJson, postJson, type RequestOptions } from './client'
import type { AdminUser, AuthCapability, CurrentUser, SignupResult } from './types'

/**
 * Whether this deployment offers a password login, and whether it is open to
 * new accounts.
 *
 * Unauthenticated on purpose: the login page has to ask before anybody has
 * logged in. Asked rather than inferred from the build, because the switches
 * live on the server and a bundle cannot know what a deployment turned on.
 */
export function fetchAuthCapability(options: RequestOptions = {}): Promise<AuthCapability> {
  return getJson<AuthCapability>('/auth/capability', options)
}

/**
 * Who is logged in.
 *
 * Throws ApiClientError with isUnauthenticated when nobody is, which is the
 * normal answer on a first visit rather than a failure worth reporting.
 */
export function fetchCurrentUser(options: RequestOptions = {}): Promise<CurrentUser> {
  return getJson<CurrentUser>('/auth/me', options)
}

/**
 * Create an account.
 *
 * Returns a pending status, not a session. The account has to be approved by
 * an administrator before it can be used, so there is nothing to log in to
 * yet -- and the response says so in words the page can show directly.
 */
export function signup(
  body: { login_id: string; name: string; password: string; password_confirm: string },
  options: RequestOptions = {},
): Promise<SignupResult> {
  return postJson<SignupResult>('/auth/signup', body, options)
}

/**
 * Log in.
 *
 * The session arrives as an HttpOnly cookie, which this code can neither read
 * nor store -- and neither can any script that gets injected into the page.
 * There is nothing to keep in localStorage and nothing to attach to later
 * requests; the browser sends the cookie because it is same-origin.
 */
export function login(
  body: { login_id: string; password: string },
  options: RequestOptions = {},
): Promise<CurrentUser> {
  return postJson<CurrentUser>('/auth/login', body, options)
}

/** Revokes the session server-side and clears the cookie. */
export function logout(options: RequestOptions = {}): Promise<void> {
  return postJson<void>('/auth/logout', {}, options)
}

// --- administration --------------------------------------------------------

export function fetchAdminUsers(
  status?: string, options: RequestOptions = {},
): Promise<AdminUser[]> {
  return getJson<AdminUser[]>('/admin/users', { ...options, params: status ? { status } : {} })
}

/**
 * Let somebody in.
 *
 * No body: approval is one decision -- may this person sign in -- and it
 * grants no document permission of its own.
 */
export function approveUser(userId: string, options: RequestOptions = {}): Promise<AdminUser> {
  return postJson<AdminUser>(
    `/admin/users/${encodeURIComponent(userId)}/approve`, {}, options,
  )
}

export function disableUser(userId: string, options: RequestOptions = {}): Promise<AdminUser> {
  return postJson<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/disable`, {}, options)
}

export function setUserAdmin(
  userId: string, granted: boolean, options: RequestOptions = {},
): Promise<AdminUser> {
  return postJson<AdminUser>(
    `/admin/users/${encodeURIComponent(userId)}/admin`, { granted }, options,
  )
}

/**
 * Change one's own password.
 *
 * The current password is required even though the caller is signed in: an
 * unattended browser is a session, not a person. Every other session of this
 * user ends; the one making the change does not.
 */
export function changePassword(
  body: { current_password: string; new_password: string; new_password_confirm: string },
  options: RequestOptions = {},
): Promise<void> {
  return postJson<void>('/auth/password', body, options)
}
