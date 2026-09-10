"""Injection point for a company-approved provider. No SDK, URL, or downloads."""
import os
from typing import Protocol

from .exceptions import GenerationUnavailable
from .prompts import GenerationRequest

#: The selector value that means "no provider". Also what an unset variable
#: resolves to, so the safe state is the default state.
UNCONFIGURED = 'unconfigured'


def selected_provider_name() -> str:
    """Which provider LLM_PROVIDER names, normalised.

    The single reader of that variable's meaning. Deciding "is generation on?"
    in more than one place is how a summary worker and an API end up
    disagreeing -- one queueing work the other will never do.
    """
    return os.environ.get('LLM_PROVIDER', '').strip().lower() or UNCONFIGURED


def generation_enabled() -> bool:
    """Whether any text generation can happen at all in this deployment.

    A credential in the environment is not enough: the selector has to name a
    provider. This is only the first of two gates -- it says a provider could
    be reached, not that internal documents may be sent to it.
    """
    return selected_provider_name() != UNCONFIGURED


#: Second gate. Selecting a provider says a service is reachable; this says
#: this corpus may be sent to it. They are separate because they are different
#: decisions made by different people at different times: wiring up a vendor is
#: a deployment task, while permitting internal documents to leave the network
#: is an approval. Nothing about having a vendor configured, or a key present,
#: implies that approval has been given.
EXTERNAL_DOCUMENT_LLM_FLAG = 'DOCUMENT_EXTERNAL_LLM_ENABLED'

#: Exactly the values that mean yes. Anything else -- unset, empty, 'no',
#: 'maybe', a typo -- is no. The failure mode of a permissive parser here is
#: sending real internal documents to a third party.
_TRUE_VALUES = frozenset({'1', 'true', 'yes', 'on'})


def external_document_llm_enabled() -> bool:
    """Whether documents from this corpus may be sent to an external provider.

    Default false, and false is reached by every path except one explicit
    opt-in value. Intended to stay false wherever the real internal corpus is
    mounted, and to be turned on only in an environment holding synthetic or
    non-sensitive documents.
    """
    return os.environ.get(EXTERNAL_DOCUMENT_LLM_FLAG, '').strip().lower() in _TRUE_VALUES


def document_generation_enabled() -> bool:
    """The single question every document-text feature must ask.

    Both gates, in one place, so summary generation and document-scoped chat
    cannot drift apart -- one of them being stricter than the other would make
    the looser one the real policy.

    Every caller that is about to put document text into a provider request
    checks this. Read it as: a provider is configured, *and* this deployment is
    allowed to send it our documents.
    """
    return generation_enabled() and external_document_llm_enabled()


class LLMProvider(Protocol):
    identifier: str

    def generate(self, request: GenerationRequest) -> object:
        """Return GenerationResult, a matching dict, or strict JSON.

        Implementations must preserve the separate instruction/question/data
        fields, enforce their inference timeout, and obey company data policy.
        """
        ...


class UnconfiguredProvider:
    identifier = UNCONFIGURED

    def generate(self, request: GenerationRequest) -> object:
        raise GenerationUnavailable('No approved provider configured')
