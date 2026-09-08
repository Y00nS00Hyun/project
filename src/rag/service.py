"""Authenticate upstream, retrieve with existing ACL search, generate, validate, persist."""
import logging
import time

from search.models import SearchMode, SearchRequest
from search.service import SearchService

from .context import RagConfig, assemble_context
from .exceptions import GenerationRateLimited, GenerationUnavailable
from .prompts import generation_request
from .provider import LLMProvider
from .repository import ChatRepository
from .validation import refusal, validate_generation

logger = logging.getLogger('rag')


class ChatService:
    """Session operations do not depend on an embedding model or LLM provider."""
    def __init__(self, repository: ChatRepository):
        self.repository = repository

    def create_session(self, user_id: str, title: str | None):
        return self.repository.create_session(user_id, title)

    def list_sessions(self, user_id: str, page: int, size: int):
        return self.repository.list_sessions(user_id, page, size)

    def get_session(self, user_id: str, session_id: str, page: int, size: int):
        return self.repository.get_session(user_id, session_id, page, size)


class RagService:
    def __init__(self, repository: ChatRepository, search: SearchService, provider: LLMProvider, config: RagConfig):
        self.repository = repository
        self.search = search
        self.provider = provider
        self.config = config

    def send_message(self, user_id: str, session_id: str, question: str, request_id: str):
        started = time.perf_counter()
        self.repository.require_session(user_id, session_id)
        try:
            matches = self.search.search(SearchRequest(
                user_id=user_id, query=question, mode=SearchMode.SEMANTIC,
                page=1, size=self.config.retrieval_limit,
            ))
            ids = [item.matched_chunk.chunk_id for item in matches.items if item.matched_chunk is not None]
            chunks = self.repository.load_context(user_id, ids, self.config.max_context_chars)
            context = assemble_context(chunks, self.config)
            if not context:
                answer = refusal()
            else:
                raw = self.provider.generate(generation_request(question, context))
                answer = validate_generation(raw, context)
        except GenerationRateLimited:
            # Already detail-free, and the contract has an existing code for it.
            raise
        except Exception:
            # Provider exception strings can include secrets, prompts or text.
            # Never propagate their messages or attach traceback to an info log.
            raise GenerationUnavailable() from None
        duration = round((time.perf_counter() - started) * 1000)
        result = self.repository.save_turn(
            user_id, session_id, question, answer, context, self.provider.identifier, duration,
        )
        logger.info('rag.completed', extra={
            'request_id': request_id, 'session_id': session_id,
            'message_id': str(result['message_id']), 'context_count': len(context),
            'refused': result['refused'], 'latency_ms': duration,
            'provider': self.provider.identifier,
        })
        return result
