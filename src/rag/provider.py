"""Injection point for a company-approved provider. No SDK, URL, or downloads."""
from typing import Protocol

from .exceptions import GenerationUnavailable
from .prompts import GenerationRequest


class LLMProvider(Protocol):
    identifier: str

    def generate(self, request: GenerationRequest) -> object:
        """Return GenerationResult, a matching dict, or strict JSON.

        Implementations must preserve the separate instruction/question/data
        fields, enforce their inference timeout, and obey company data policy.
        """
        ...


class UnconfiguredProvider:
    identifier = 'unconfigured'

    def generate(self, request: GenerationRequest) -> object:
        raise GenerationUnavailable('No approved provider configured')
