import type { TagRef } from './api/types'

/**
 * Document kind, carried as a tag under a reserved namespace.
 *
 * The backend stores kinds in the same `tags` table as free-form tags, so the
 * existing `tag_id` search filter and GET /api/v1/tags work unchanged and
 * neither the schema nor the API contract had to move. The namespace is what
 * keeps the two apart.
 */
export const TAG_NAMESPACE = '종류:'

export interface DocumentTypeOption {
  /** tags.id -- what the search filter actually sends. */
  id: number
  /** The tag name with the namespace stripped, e.g. "요구사항 정의서". */
  label: string
}

/** Display order. Anything unrecognised keeps server order, and 기타 sinks. */
const ORDER = ['매뉴얼', '요구사항 정의서', '제안·입찰 문서', '보고서', '기타']

export function isDocumentTypeTag(tag: TagRef): boolean {
  return tag.name.startsWith(TAG_NAMESPACE)
}

/**
 * Split a tag list into document kinds and everything else.
 *
 * The label is the tag name minus the prefix -- the backend stores the display
 * text verbatim after the namespace, so there is no mapping table here that
 * could drift out of step with the classifier.
 */
export function documentTypeOptions(tags: TagRef[]): DocumentTypeOption[] {
  return tags
    .filter(isDocumentTypeTag)
    .map((tag) => ({ id: tag.id, label: tag.name.slice(TAG_NAMESPACE.length) }))
    .sort((a, b) => {
      const ai = ORDER.indexOf(a.label)
      const bi = ORDER.indexOf(b.label)
      if (ai === -1 && bi === -1) return a.label.localeCompare(b.label, 'ko')
      if (ai === -1) return 1
      if (bi === -1) return -1
      return ai - bi
    })
}

/** Free-form tags, i.e. everything that is not a document kind. */
export function freeFormTags(tags: TagRef[]): TagRef[] {
  return tags.filter((tag) => !isDocumentTypeTag(tag))
}
