import type { LoadLimits } from '../types'

/** `SprintResponse.load_limits` for fixtures — one copy, not one per suite. */
export const LOAD_LIMITS: LoadLimits = {
  max_users: 10,
  max_total_requests: 2000,
  max_duration_seconds: 60,
  safe_methods: ['GET', 'HEAD', 'OPTIONS'],
}
