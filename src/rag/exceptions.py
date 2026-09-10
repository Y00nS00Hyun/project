class SessionNotFound(Exception):
    """Missing, malformed, or another user's session; deliberately identical."""


class GenerationUnavailable(Exception):
    """A provider/retrieval failure whose internal detail must not be exposed."""


class ProviderConfigurationError(Exception):
    """The selected provider is not usable. Fail closed rather than guess.

    Never carries the API key or any other credential in its message.
    """


class GenerationRateLimited(Exception):
    """Upstream provider rate limit.

    Distinct from GenerationUnavailable only because API Contract v1 already
    defines RATE_LIMITED (429); no new error code is introduced for it.
    """


class DocumentScopeNotFound(Exception):
    """The document a session is (or would be) scoped to is not readable.

    Raised both when creating a scoped session and when asking a question in
    one, because permission can be revoked between the two. Deliberately does
    not distinguish "no such document" from "no permission": telling them apart
    would confirm the existence of documents the caller cannot read.
    """


class GenerationDisabled(Exception):
    """Answer generation is switched off in this deployment.

    Distinct from GenerationUnavailable, which means a configured provider
    failed. This one means no provider may be called at all -- either none is
    selected, or this corpus is not cleared to be sent to one. It is a
    configuration state, not an incident: it will not resolve on retry, so it
    must not be reported as a server error.
    """
