import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import NonfunctionalRunDetailPage from './NonfunctionalRunDetailPage'
import type {
  NonfunctionalLoadProfileResponse,
  NonfunctionalRunDetailResponse,
  NonfunctionalTargetResponse,
} from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchNonfunctionalRun: vi.fn(),
    fetchSprint: vi.fn(),
    restartNonfunctionalRun: vi.fn(),
    summarizeNonfunctionalRun: vi.fn(),
    exportNonfunctionalRunFindings: vi.fn(),
  }
})

import { fetchNonfunctionalRun, fetchSprint } from '../services/api'

const mockFetchRun = fetchNonfunctionalRun as ReturnType<typeof vi.fn>
const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>

function makeTarget(
  overrides: Partial<NonfunctionalTargetResponse> = {},
): NonfunctionalTargetResponse {
  return {
    id: 1,
    position: 0,
    url: 'https://app.test/login',
    kind: 'page',
    status: 'completed',
    error: null,
    a11y_outcome: 'violations',
    security_outcome: 'clean',
    performance_outcome: 'clean',
    metrics: { load_ms: 340 },
    finding_count: 1,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeProfile(
  overrides: Partial<NonfunctionalLoadProfileResponse> = {},
): NonfunctionalLoadProfileResponse {
  return {
    id: 1,
    position: 0,
    url: 'https://app.test/api/items',
    method: 'GET',
    body: null,
    concurrency: 2,
    duration_seconds: 10,
    total_request_cap: 50,
    status: 'completed',
    requests_sent: 50,
    results: { p95_ms: 180 },
    error: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeRun(
  overrides: Partial<NonfunctionalRunDetailResponse> = {},
): NonfunctionalRunDetailResponse {
  return {
    id: 3,
    sprint_id: 1,
    requirement_id: 5,
    requirement_name: 'Login',
    status: 'completed',
    domains: ['accessibility', 'security', 'performance'],
    environment_disposable: false,
    summary: 'Two accessibility violations on the login page.',
    error: null,
    outdated_reasons: [],
    requirement_deleted: false,
    target_count: 1,
    load_profile_count: 0,
    bug_count: 1,
    issue_count: 0,
    high_severity_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    base_url_env_vars: ['BASE_URL'],
    targets: [makeTarget()],
    load_profiles: [],
    findings: [
      {
        id: 10,
        position: 0,
        domain: 'accessibility',
        rule: 'image-alt',
        url: 'https://app.test/login',
        has_screenshot: false,
        created_at: '2026-01-01T00:00:00Z',
        finding_type: 'bug',
        severity: 'high',
        title: 'Images have no alternative text',
        steps_to_reproduce: 'Open the login page',
        expected: 'Every image carries alt text',
        actual: '2 images have no alt attribute',
        environment: null,
        tracker_issue_key: null,
        tracker_issue_url: null,
        tracker_error: null,
        tracker_is_duplicate: false,
      },
    ],
    export_findings: false,
    exported_finding_count: 0,
    exported_issue_count: 0,
    export_error_count: 0,
    unexported_finding_count: 0,
    export_groups: [],
    ...overrides,
  }
}

function renderPage() {
  const router = createMemoryRouter(
    [{ path: '/sprints/:id/nonfunctional-runs/:runId', element: <NonfunctionalRunDetailPage /> }],
    { initialEntries: ['/sprints/1/nonfunctional-runs/3'] },
  )
  return render(<RouterProvider router={router} />)
}

describe('NonfunctionalRunDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockFetchSprint.mockResolvedValue({ id: 1, name: 'Sprint 1', active: true })
  })

  it('renders findings with their rule and URL', async () => {
    mockFetchRun.mockResolvedValue(makeRun())
    renderPage()

    await waitFor(() =>
      expect(screen.getByText('Images have no alternative text')).toBeInTheDocument(),
    )
    expect(screen.getByText('image-alt')).toBeInTheDocument()
    expect(screen.getAllByText('https://app.test/login').length).toBeGreaterThan(0)
  })

  it('shows a per-domain outcome for every target', async () => {
    mockFetchRun.mockResolvedValue(makeRun())
    renderPage()

    await waitFor(() => expect(screen.getByText('Violations found')).toBeInTheDocument())
    expect(screen.getAllByText('No violations')).toHaveLength(2)
    expect(screen.getByText('load ms')).toBeInTheDocument()
    expect(screen.getByText('340')).toBeInTheDocument()
  })

  it('distinguishes could-not-run from clean', async () => {
    mockFetchRun.mockResolvedValue(
      makeRun({
        targets: [
          makeTarget({
            a11y_outcome: 'failed_to_run',
            security_outcome: 'not_applicable',
            performance_outcome: null,
            error: 'axe could not run on this page',
          }),
        ],
      }),
    )
    renderPage()

    await waitFor(() => expect(screen.getByText('Could not run')).toBeInTheDocument())
    expect(screen.getByText('Not applicable here')).toBeInTheDocument()
    // Null is a fourth answer: the domain was never selected for this run.
    expect(screen.getByText('Not selected')).toBeInTheDocument()
    expect(screen.queryByText('No violations')).not.toBeInTheDocument()
  })

  it('labels a load profile as authenticated and says what it sent', async () => {
    mockFetchRun.mockResolvedValue(
      makeRun({ load_profiles: [makeProfile()], load_profile_count: 1 }),
    )
    renderPage()

    await waitFor(() => expect(screen.getByText(/50 requests sent of 50/)).toBeInTheDocument())
    expect(screen.getByText(/ran authenticated/)).toBeInTheDocument()
    expect(screen.getByText('p95 ms')).toBeInTheDocument()
  })

  it('renders an empty state rather than a blank panel when nothing was reached', async () => {
    mockFetchRun.mockResolvedValue(
      makeRun({ targets: [], findings: [], target_count: 0, bug_count: 0 }),
    )
    renderPage()

    await waitFor(() =>
      expect(screen.getByText('This run reached no URL to examine.')).toBeInTheDocument(),
    )
    expect(screen.getByText('No violations were found.')).toBeInTheDocument()
  })

  it('offers a summary retry when the best-effort summary is missing', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ summary: null }))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Generate summary' })).toBeInTheDocument(),
    )
  })

  it('offers Restart only on a failed run', async () => {
    mockFetchRun.mockResolvedValue(makeRun({ status: 'failed', error: 'worker died' }))
    renderPage()

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Restart run' })).toBeInTheDocument(),
    )
    expect(screen.getByText('worker died')).toBeInTheDocument()
  })

  it('polls while the run is still working', async () => {
    vi.useFakeTimers()
    try {
      mockFetchRun.mockResolvedValue(makeRun({ status: 'running' }))
      renderPage()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2500)
      })
      expect(mockFetchRun).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
