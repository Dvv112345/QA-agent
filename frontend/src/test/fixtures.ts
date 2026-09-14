import type { LoadLimits } from '../types'

/** `SprintResponse.load_limits` for fixtures — one copy, not one per suite. */
export const LOAD_LIMITS: LoadLimits = {
  max_users: 10,
  max_total_requests: 2000,
  max_duration_seconds: 60,
  max_soak_duration_seconds: 900,
  stress_min_duration_seconds: 50,
  safe_methods: ['GET', 'HEAD', 'OPTIONS'],
  load_shapes: ['load', 'stress', 'spike', 'soak'],
}
