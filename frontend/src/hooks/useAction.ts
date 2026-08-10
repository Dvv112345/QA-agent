import { useCallback, useState } from 'react'

/**
 * Run a one-shot mutation, tracking its in-flight and error state.
 *
 * The cards and detail pages each had their own copy of this: set busy,
 * clear the error, call, hand the result on, catch into an error string,
 * clear busy. Three copies meant three chances to get the reset ordering
 * subtly different.
 *
 * The error is cleared when a new attempt *starts*, not when one succeeds,
 * so a failure stays on screen until the user actually retries.
 */
export function useAction<T>(onSuccess: (value: T) => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * `also` runs before the shared `onSuccess` and is for the caller's own
   * local cleanup — clearing the textarea whose contents were just
   * submitted, closing an editor. It only runs on success, which is the
   * point: a failed submit must not discard what the user typed.
   */
  const run = useCallback(
    (promise: Promise<T>, also?: (value: T) => void) => {
      setBusy(true)
      setError(null)
      return promise
        .then((value) => {
          also?.(value)
          onSuccess(value)
        })
        .catch((err: Error) => setError(err.message))
        .finally(() => setBusy(false))
    },
    [onSuccess],
  )

  // `setBusy`/`setError` are exposed for the odd mutation whose shape does
  // not fit `run` — a delete that unmounts the component on success, and so
  // deliberately never clears `busy`. Prefer `run` everywhere else.
  return { busy, error, run, setBusy, setError }
}
