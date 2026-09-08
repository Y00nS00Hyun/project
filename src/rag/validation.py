from pydantic import ValidationError

from .models import EvidenceChunk, GenerationResult, ValidatedAnswer

REFUSAL_TEXT = '관련 문서에서 확인할 수 없습니다.'


def refusal() -> ValidatedAnswer:
    return ValidatedAnswer(REFUSAL_TEXT, True)


def validate_generation(raw: object, context: tuple[EvidenceChunk, ...]) -> ValidatedAnswer:
    try:
        if isinstance(raw, str):
            result = GenerationResult.model_validate_json(raw)
        else:
            result = GenerationResult.model_validate(raw)
    except (ValidationError, ValueError, TypeError):
        return refusal()
    allowed = {chunk.chunk_id for chunk in context}
    cited = tuple(dict.fromkeys(result.citation_chunk_ids))
    # Reject the ENTIRE generation if even one invented citation is present.
    # Dropping only the bad ID could leave an answer relying on invented evidence.
    if any(chunk_id not in allowed for chunk_id in cited):
        return refusal()
    if not result.answerable:
        return ValidatedAnswer(REFUSAL_TEXT, True, cited)
    if not cited or not result.answer.strip():
        return refusal()
    return ValidatedAnswer(result.answer, False, cited)
