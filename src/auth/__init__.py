"""Development-only local authentication.

A stand-in for the company SSO that does not exist yet, so that the application
can be used from a browser by a real person instead of by a header set with
curl. It is not an operational authentication system and is not intended to
become one: when SSO arrives, this package is deleted.

That intent is enforced rather than documented. Local auth is unreachable
unless LOCAL_AUTH_ENABLED is explicitly true *and* the deployment is not
production, and neither an APP_ENV value nor the presence of any credential
turns it on by itself.
"""
