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
