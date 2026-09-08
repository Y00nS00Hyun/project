import { useEffect, useRef, useState } from 'react'
import { ApiClientError } from '../api/client'

export interface AsyncResource<T> {
  data: T | null
  error: ApiClientError | null
  loading: boolean
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/**
 * Run an API call whenever `deps` change, keeping only the newest result.
 *
 * Two guards, because they fail differently: the AbortController cancels the
 * in-flight request, and the `requestId` check discards a response that had
 * already resolved before the abort landed. Without the second one, a slow
 * search for an old query can overwrite the results of a newer one.
 */
export function useAsyncResource<T>(
  run: (signal: AbortSignal) => Promise<T>,
  deps: readonly unknown[],
  enabled = true,
): AsyncResource<T> {
  const [state, setState] = useState<AsyncResource<T>>({
    data: null,
    error: null,
    loading: enabled,
  })
  const latest = useRef(0)

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false })
      return
    }
    const requestId = ++latest.current
    const controller = new AbortController()
    setState({ data: null, error: null, loading: true })

    run(controller.signal)
      .then((data) => {
        if (controller.signal.aborted || requestId !== latest.current) return
        setState({ data, error: null, loading: false })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || requestId !== latest.current || isAbort(error)) return
        setState({
          data: null,
          error:
            error instanceof ApiClientError
              ? error
              : new ApiClientError('INTERNAL_ERROR', '알 수 없는 오류가 발생했습니다.', 0, null),
          loading: false,
        })
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled])

  return state
}
