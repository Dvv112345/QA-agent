import { useEffect, useRef } from 'react'

/**
 * How often a page in progress re-reads its data. The backend has no push
 * channel, so every live page polls; one interval for all of them means the
 * app has one refresh rhythm rather than six that happen to agree.
 */
export const POLL_INTERVAL_MS = 2500

/**
 * How many ticks to keep polling a run that has finished but whose findings
 * have not reached the tracker yet (~2 minutes).
 *
 * The window exists because export runs *after* the commit that marks a run
 * `completed`: the run reads terminal while its tickets are still being
 * filed, so a page that stops polling on "not running" tears its interval
 * down inside that window and shows the export as never having happened.
 * Bounded rather than unbounded because nothing will ever say "the export
 * definitely failed" — a tracker outage just leaves the counts where they
 * are, and polling forever would be the alternative.
 */
export const EXPORT_GRACE_TICKS = 48

interface Options {
  /** Poll only while this is true. Flipping it to false clears the interval. */
  enabled: boolean
  /**
   * Stop after this many ticks. Omit for unbounded — correct while the work
   * itself is running, since the work is what says when it ends.
   */
  maxTicks?: number
}

/**
 * Poll `fetcher` on the shared interval while `enabled`.
 *
 * Owns the three things every page's hand-written version had to get right:
 * an in-flight guard so a slow response cannot stack requests, a swallowed
 * catch so one transient failure does not kill the interval, and cleanup on
 * unmount.
 *
 * `fetcher` is held in a ref, so a caller may pass an inline closure without
 * restarting the interval on every render.
 */
export function usePolling(fetcher: () => Promise<unknown>, { enabled, maxTicks }: Options): void {
  const fetcherRef = useRef(fetcher)
  useEffect(() => {
    fetcherRef.current = fetcher
  })

  const inFlightRef = useRef(false)

  useEffect(() => {
    if (!enabled) return

    let ticksLeft = maxTicks ?? Number.POSITIVE_INFINITY
    const pollId = setInterval(() => {
      if (inFlightRef.current) return
      if (ticksLeft <= 0) {
        clearInterval(pollId)
        return
      }
      ticksLeft -= 1
      inFlightRef.current = true
      fetcherRef
        .current()
        .catch(() => {
          /* transient poll failure — retry on next tick */
        })
        .finally(() => {
          inFlightRef.current = false
        })
    }, POLL_INTERVAL_MS)

    return () => clearInterval(pollId)
  }, [enabled, maxTicks])
}
