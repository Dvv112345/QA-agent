import { useEffect, useState } from 'react'

export interface AsyncData<T> {
  data: T | null
  loading: boolean
  error: string | null
  /** For pages that mutate the loaded data in place after an action. */
  setData: React.Dispatch<React.SetStateAction<T | null>>
}

/**
 * Load data once on mount (and whenever `deps` change), with the
 * cancellation guard baked in.
 *
 * Ten pages hand-rolled this, in two different shapes — some wrapping every
 * setState in `if (!cancelled)`, some returning early on `if (cancelled)`.
 * Same intent, but a reader had to re-verify each one, and the guard is the
 * part that is easy to get wrong: without it a response arriving after
 * navigation sets state on an unmounted page.
 *
 * `deps` is the caller's dependency list, passed through unchanged — pass
 * the ids the fetch is keyed on, exactly as you would to `useEffect`.
 */
export function useAsyncData<T>(fetcher: () => Promise<T>, deps: unknown[]): AsyncData<T> {
  const [data, setData] = useState<T | null>(null)
  // Starts true and is only ever cleared. A refetch triggered by changed
  // `deps` therefore keeps showing the previous data rather than flashing a
  // loading state — which is what the hand-written effects this replaces
  // did, since none of them reset the flag either.
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetcher()
      .then((value) => {
        if (cancelled) return
        setData(value)
        setLoading(false)
      })
      .catch((err: Error) => {
        if (cancelled) return
        setError(err.message)
        setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // The fetcher is a fresh closure each render; `deps` is what actually
    // decides when to refetch, exactly as the hand-written effects did.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { data, loading, error, setData }
}
