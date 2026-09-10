"""The two switches that decide whether a document may leave this network.

Selecting a provider says a service is reachable. Permitting this corpus to be
sent to it is a separate decision, made by different people at a different
time, and nothing about a vendor being configured -- or a key being present --
implies it has been made.

Both document-text features read the same answer, so neither can be looser than
the other. These tests are what keep that true.
"""

from __future__ import annotations

import pytest

from rag.provider import (
    EXTERNAL_DOCUMENT_LLM_FLAG,
    UnconfiguredProvider,
    document_generation_enabled,
    external_document_llm_enabled,
    generation_enabled,
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv('LLM_PROVIDER', raising=False)
    monkeypatch.delenv(EXTERNAL_DOCUMENT_LLM_FLAG, raising=False)


def test_the_default_is_off(monkeypatch):
    assert external_document_llm_enabled() is False
    assert document_generation_enabled() is False


@pytest.mark.parametrize('value', ['1', 'true', 'TRUE', 'True', 'yes', 'on', ' true '])
def test_explicit_opt_in_values(monkeypatch, value):
    monkeypatch.setenv(EXTERNAL_DOCUMENT_LLM_FLAG, value)
    assert external_document_llm_enabled() is True


@pytest.mark.parametrize('value', [
    '', ' ', '0', 'false', 'no', 'off', 'null', 'none', 'enabled', 'ture', 'y', 'sure',
])
def test_everything_else_is_off(monkeypatch, value):
    # A permissive parser here sends real internal documents to a third party,
    # so anything that is not one of the accepted words means no -- including a
    # typo of one of them.
    monkeypatch.setenv(EXTERNAL_DOCUMENT_LLM_FLAG, value)
    assert external_document_llm_enabled() is False


def test_a_provider_alone_does_not_open_the_gate(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    assert generation_enabled() is True
    # Configured, reachable, and still not permitted.
    assert document_generation_enabled() is False


def test_an_api_key_alone_does_not_open_the_gate(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'not-a-real-key')
    assert document_generation_enabled() is False
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    # A key plus a vendor is a working client, not an approval.
    assert document_generation_enabled() is False


def test_the_gate_alone_does_not_invent_a_provider(monkeypatch):
    monkeypatch.setenv(EXTERNAL_DOCUMENT_LLM_FLAG, 'true')
    assert document_generation_enabled() is False


def test_both_together(monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'anthropic')
    monkeypatch.setenv(EXTERNAL_DOCUMENT_LLM_FLAG, 'true')
    assert document_generation_enabled() is True


def test_the_default_provider_still_cannot_call_out():
    with pytest.raises(Exception):
        UnconfiguredProvider().generate(object())


# ---------------------------------------------------------------------------
# Both features ask the same question
# ---------------------------------------------------------------------------

def test_summary_and_chat_read_the_same_gate():
    """Not "similar checks" -- literally the same function.

    Two independent checks would eventually disagree, and the looser of the two
    would silently become the real policy.
    """
    import inspect

    from api import document_service
    from ingestion import embedding_service
    from rag import service as rag_service
    from rag import summary_service

    sources = [
        inspect.getsource(embedding_service.EmbeddingService._request_summary),
        inspect.getsource(summary_service.SummaryService._process_job),
        inspect.getsource(summary_service.SummaryService.reconcile),
        inspect.getsource(rag_service.RagService.send_message),
        inspect.getsource(document_service._document_generation_enabled),
    ]
    for source in sources:
        assert 'document_generation_enabled' in source
        # generation_enabled() on its own is the provider half only, and using
        # it here would let documents out with the approval switch off.
        assert 'not generation_enabled()' not in source


def test_deployment_default_is_off():
    """compose.yaml and .env.example must not ship an open gate."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    compose = (root / 'compose.yaml').read_text()
    assert f'{EXTERNAL_DOCUMENT_LLM_FLAG}: ${{{EXTERNAL_DOCUMENT_LLM_FLAG}:-false}}' in compose
    assert f'{EXTERNAL_DOCUMENT_LLM_FLAG}=false' in (root / '.env.example').read_text()


def test_the_live_env_file_keeps_the_gate_shut():
    """This machine has the real internal corpus mounted."""
    from pathlib import Path

    env = Path(__file__).resolve().parents[1] / '.env'
    if not env.is_file():
        pytest.skip('no local .env')
    for line in env.read_text().splitlines():
        if line.startswith(f'{EXTERNAL_DOCUMENT_LLM_FLAG}='):
            assert line.split('=', 1)[1].strip().lower() in ('', 'false', '0', 'no', 'off')
            return
    # Absent is also shut: compose defaults it to false.
