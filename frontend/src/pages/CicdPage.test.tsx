import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import CicdPage from './CicdPage'
import type {
  CicdCaseEntry,
  CicdConfig,
  CicdEligibility,
  CicdExport,
  SprintResponse,
} from '../types'

vi.mock('../services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../services/api')>()
  return {
    ...actual,
    fetchSprint: vi.fn(),
    fetchCicdConfig: vi.fn(),
    fetchCicdEligibility: vi.fn(),
    fetchCicdExports: vi.fn(),
    createCicdExport: vi.fn(),
    restartCicdExport: vi.fn(),
    saveCicdConfig: vi.fn(),
    deleteCicdConfig: vi.fn(),
  }
})

import {
  createCicdExport,
  fetchCicdConfig,
  fetchCicdEligibility,
  fetchCicdExports,
  fetchSprint,
  restartCicdExport,
  saveCicdConfig,
} from '../services/api'

const mockFetchSprint = fetchSprint as ReturnType<typeof vi.fn>
const mockFetchConfig = fetchCicdConfig as ReturnType<typeof vi.fn>
const mockFetchEligibility = fetchCicdEligibility as ReturnType<typeof vi.fn>
const mockFetchExports = fetchCicdExports as ReturnType<typeof vi.fn>
const mockCreateExport = createCicdExport as ReturnType<typeof vi.fn>
const mockRestartExport = restartCicdExport as ReturnType<typeof vi.fn>
const mockSaveConfig = saveCicdConfig as ReturnType<typeof vi.fn>

function makeSprint(overrides: Partial<SprintResponse> = {}): SprintResponse {
  return {
    id: 1,
    name: 'Sprint One',
    repo_id: 1,
    active: true,
    directory: 'dir',
    created_at: '2026-08-18T10:00:00Z',
    repo: {
      id: 1,
      github_link: 'https://github.com/owner/repo',
      name: 'owner/repo',
      description: null,
      active: true,
      has_access_token: true,
      created_at: '2026-08-18T10:00:00Z',
    },
    requirements_complete: true,
    has_test_environment_submission: true,
    environment_confirmed: true,
    has_test_plans: true,
    test_plans_missing: false,
    test_plans_complete: true,
    has_test_runs: true,
    has_exploratory_runs: false,
    ...overrides,
  }
}

function makeEntry(overrides: Partial<CicdCaseEntry> = {}): CicdCaseEntry {
  return {
    test_case_id: 1,
    case_title: 'Happy path',
    requirement_id: 1,
    requirement_name: 'User login',
    eligible: true,
    reason: null,
    stale_reasons: [],
    previously_exported: false,
    last_export_pr_url: null,
    ...overrides,
  }
}

function makeEligibility(overrides: Partial<CicdEligibility> = {}): CicdEligibility {
  return {
    sprint_id: 1,
    entries: [makeEntry()],
    eligible_count: 1,
    stale_count: 0,
    no_script_count: 0,
    variable_names: [{ name: 'BASE_URL', env_var: 'BASE_URL' }],
    secret_names: [{ name: 'QA_PASSWORD', env_var: 'QA_PASSWORD' }],
    ...overrides,
  }
}

function makeConfig(overrides: Partial<CicdConfig> = {}): CicdConfig {
  return {
    id: 1,
    sprint_id: 1,
    provider: 'github_actions',
    ci_environment_hint: null,
    verified_at: '2026-08-18T10:00:00Z',
    created_at: '2026-08-18T10:00:00Z',
    updated_at: '2026-08-18T10:00:00Z',
    ...overrides,
  }
}

function makeExport(overrides: Partial<CicdExport> = {}): CicdExport {
  return {
    id: 1,
    sprint_id: 1,
    provider: 'github_actions',
    status: 'completed',
    branch_name: 'qa-agent/sprint-1-20260818-100000-ab12',
    commit_sha: 'abc123',
    pr_number: 7,
    pr_url: 'https://github.com/owner/repo/pull/7',
    pr_title: 'Add the QA suite',
    notes: null,
    error: null,
    case_count: 1,
    ci_file_paths: ['.github/workflows/qa-agent.yml'],
    dropped_paths: [],
    // The receipt records the CI-side names only — what the team was asked
    // to create, not the sprint's own vocabulary.
    variable_names: ['BASE_URL'],
    secret_names: ['QA_PASSWORD'],
    items: [],
    created_at: '2026-08-18T10:00:00Z',
    updated_at: '2026-08-18T10:05:00Z',
    ...overrides,
  }
}

function renderPage() {
  const router = createMemoryRouter([{ path: '/sprints/:id/cicd', element: <CicdPage /> }], {
    initialEntries: ['/sprints/1/cicd'],
  })
  return render(<RouterProvider router={router} />)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockFetchSprint.mockResolvedValue(makeSprint())
  mockFetchConfig.mockResolvedValue(makeConfig())
  mockFetchEligibility.mockResolvedValue(makeEligibility())
  mockFetchExports.mockResolvedValue([])
})

afterEach(() => {
  vi.useRealTimers()
})

describe('CicdPage', () => {
  it('renders the eligibility list', async () => {
    renderPage()

    expect(await screen.findByText('Happy path')).toBeInTheDocument()
    expect(screen.getByText('User login')).toBeInTheDocument()
  })

  it('renders ineligible rows disabled, with their reason', async () => {
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        entries: [makeEntry({ eligible: false, reason: 'no_script' })],
        eligible_count: 0,
        no_script_count: 1,
      }),
    )

    renderPage()

    expect(await screen.findByText(/No script yet/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('distinguishes stale from no_script, because they imply different actions', async () => {
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        entries: [
          makeEntry({ test_case_id: 1, case_title: 'Unrun', eligible: false, reason: 'no_script' }),
          makeEntry({
            test_case_id: 2,
            case_title: 'Stale',
            eligible: false,
            reason: 'stale',
            stale_reasons: ['requirement'],
          }),
        ],
        eligible_count: 0,
      }),
    )

    renderPage()

    expect(await screen.findByText(/run this test case first/)).toBeInTheDocument()
    expect(screen.getByText(/the requirement changed/)).toBeInTheDocument()
    expect(screen.getByText(/Re-run this test case/)).toBeInTheDocument()
  })

  it('renders a script cached before change tracking as out of date', async () => {
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        entries: [makeEntry({ eligible: false, reason: 'stale', stale_reasons: ['unknown'] })],
        eligible_count: 0,
        stale_count: 1,
      }),
    )

    renderPage()

    expect(await screen.findByText(/predates change tracking/)).toBeInTheDocument()
  })

  it('starts eligible cases checked and already-exported ones unchecked', async () => {
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        entries: [
          makeEntry({ test_case_id: 1, case_title: 'Fresh' }),
          makeEntry({
            test_case_id: 2,
            case_title: 'Shipped',
            previously_exported: true,
            last_export_pr_url: 'https://github.com/owner/repo/pull/3',
          }),
        ],
        eligible_count: 2,
      }),
    )

    renderPage()

    await screen.findByText('Fresh')
    const [fresh, shipped] = screen.getAllByRole('checkbox')
    expect(fresh).toBeChecked()
    expect(shipped).not.toBeChecked()
    expect(screen.getByText('already exported')).toHaveAttribute(
      'href',
      'https://github.com/owner/repo/pull/3',
    )
  })

  it('posts exactly the checked ids', async () => {
    mockCreateExport.mockResolvedValue(makeExport({ status: 'pending' }))
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        entries: [
          makeEntry({ test_case_id: 1, case_title: 'One' }),
          makeEntry({ test_case_id: 2, case_title: 'Two' }),
        ],
        eligible_count: 2,
      }),
    )

    renderPage()
    await screen.findByText('One')
    fireEvent.click(screen.getAllByRole('checkbox')[1]) // untick "Two"
    fireEvent.click(screen.getByRole('button', { name: /^Export/ }))

    await waitFor(() => expect(mockCreateExport).toHaveBeenCalledWith(1, [1]))
  })

  it('disables Export with nothing selected', async () => {
    renderPage()
    await screen.findByText('Happy path')

    fireEvent.click(screen.getByRole('checkbox'))

    expect(screen.getByRole('button', { name: /^Export/ })).toBeDisabled()
  })

  it('disables Export and says so when no config exists', async () => {
    mockFetchConfig.mockResolvedValue(null)

    renderPage()

    expect(await screen.findByText(/Connect a CI\/CD target before exporting/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Export/ })).toBeDisabled()
  })

  it('names the variables and secrets the team must create', async () => {
    renderPage()

    expect(await screen.findByText('BASE_URL')).toBeInTheDocument()
    expect(screen.getByText('QA_PASSWORD')).toBeInTheDocument()
  })

  it('leads with the CI name and says which sprint variable it feeds', async () => {
    // `base_url` is not what the team creates on GitHub Actions — `BASE_URL`
    // is. Showing the sprint's own name alone would name something they
    // cannot create verbatim.
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({
        variable_names: [{ name: 'BASE_URL', env_var: 'base_url' }],
        secret_names: [{ name: 'QA_GITHUB_PAT', env_var: 'GITHUB_PAT' }],
      }),
    )

    renderPage()

    expect(await screen.findByText('BASE_URL')).toBeInTheDocument()
    expect(screen.getByText('for base_url')).toBeInTheDocument()
    expect(screen.getByText('QA_GITHUB_PAT')).toBeInTheDocument()
    expect(screen.getByText('for GITHUB_PAT')).toBeInTheDocument()
  })

  it('omits the source name when the two coincide', async () => {
    renderPage()

    await screen.findByText('BASE_URL')
    expect(screen.queryByText('for BASE_URL')).not.toBeInTheDocument()
  })

  it('links a history row to its pull request', async () => {
    mockFetchExports.mockResolvedValue([makeExport()])

    renderPage()

    const link = await screen.findByText('Pull request #7')
    expect(link).toHaveAttribute('href', 'https://github.com/owner/repo/pull/7')
    expect(screen.getByText('Exported')).toBeInTheDocument()
  })

  it('shows a failed export its error and a Restart control', async () => {
    mockFetchExports.mockResolvedValue([
      makeExport({
        status: 'failed',
        error: 'GitHub returned 503 — service may be degraded.',
        pr_url: null,
        pr_number: null,
      }),
    ])
    mockRestartExport.mockResolvedValue(makeExport({ status: 'pending' }))

    renderPage()

    expect(await screen.findByText(/service may be degraded/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))

    await waitFor(() => expect(mockRestartExport).toHaveBeenCalledWith(1))
  })

  it('names the dropped paths a validation gate refused', async () => {
    mockFetchExports.mockResolvedValue([makeExport({ dropped_paths: ['../etc/passwd'] })])

    renderPage()

    expect(await screen.findByText('../etc/passwd')).toBeInTheDocument()
    expect(screen.getByText(/Not written/)).toBeInTheDocument()
  })

  it('polls while an export is in flight', async () => {
    // Fake timers must be installed before render — an interval registered
    // under real timers cannot be advanced later.
    vi.useFakeTimers()
    mockFetchExports.mockResolvedValue([makeExport({ status: 'running', pr_url: null })])

    renderPage()
    await vi.waitFor(() => expect(screen.getByText('Exporting')).toBeInTheDocument())
    const before = mockFetchExports.mock.calls.length

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500)
    })

    expect(mockFetchExports.mock.calls.length).toBeGreaterThan(before)
  })

  it('does not poll once every export is terminal', async () => {
    vi.useFakeTimers()
    mockFetchExports.mockResolvedValue([makeExport({ status: 'completed' })])

    renderPage()
    await vi.waitFor(() => expect(screen.getByText('Exported')).toBeInTheDocument())
    const before = mockFetchExports.mock.calls.length

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500 * 3)
    })

    expect(mockFetchExports.mock.calls.length).toBe(before)
  })

  it('surfaces a 422 from the export route verbatim', async () => {
    mockCreateExport.mockRejectedValue(new Error('This sprint has no test-environment variables.'))

    renderPage()
    await screen.findByText('Happy path')
    fireEvent.click(screen.getByRole('button', { name: /^Export/ }))

    expect(
      await screen.findByText('This sprint has no test-environment variables.'),
    ).toBeInTheDocument()
  })

  it('shows an empty state when the sprint has no test cases', async () => {
    mockFetchEligibility.mockResolvedValue(
      makeEligibility({ entries: [], eligible_count: 0, variable_names: [], secret_names: [] }),
    )

    renderPage()

    expect(await screen.findByText(/No test cases yet/)).toBeInTheDocument()
  })

  it('opens the config modal and saves through it', async () => {
    mockSaveConfig.mockResolvedValue(makeConfig({ provider: 'jenkins' }))

    renderPage()
    await screen.findByText('Happy path')
    fireEvent.click(screen.getByRole('button', { name: 'Change' }))

    fireEvent.click(await screen.findByLabelText('Jenkins'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(mockSaveConfig).toHaveBeenCalledWith(1, {
        provider: 'jenkins',
        access_token: '',
        ci_environment_hint: '',
      }),
    )
  })

  it('surfaces the push-permission 422 from the config modal', async () => {
    mockSaveConfig.mockRejectedValue(
      new Error('This token can read the repository but cannot push to it.'),
    )

    renderPage()
    await screen.findByText('Happy path')
    fireEvent.click(screen.getByRole('button', { name: 'Change' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/cannot push to it/)).toBeInTheDocument()
  })

  it('shows an error state when loading fails', async () => {
    mockFetchSprint.mockRejectedValue(new Error('Sprint not found'))

    renderPage()

    expect(await screen.findByText('Sprint not found')).toBeInTheDocument()
  })
})
