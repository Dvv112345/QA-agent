import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import App from './App'

vi.mock('./services/api', () => ({
  checkAuthStatus: vi.fn(),
  verifyPassword: vi.fn(),
}))

import { checkAuthStatus, verifyPassword } from './services/api'

const mockCheckAuthStatus = checkAuthStatus as ReturnType<typeof vi.fn>
const mockVerifyPassword = verifyPassword as ReturnType<typeof vi.fn>

describe('App auth flow', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows LoginModal when checkAuthStatus returns valid=false', async () => {
    mockCheckAuthStatus.mockResolvedValue({ valid: false })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('QA Agent')).toBeInTheDocument()
    })
    expect(screen.getByLabelText('Access Code')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()
  })

  it('renders HomePage when checkAuthStatus returns valid=true', async () => {
    mockCheckAuthStatus.mockResolvedValue({ valid: true })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('QA Agent Upload')).toBeInTheDocument()
    })
    expect(screen.getByText(/upload & analyze/i)).toBeInTheDocument()
  })

  it('does not render router content when unauthenticated', async () => {
    mockCheckAuthStatus.mockResolvedValue({ valid: false })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByText('QA Agent')).toBeInTheDocument()
    })
    expect(screen.queryByText('QA Agent Upload')).not.toBeInTheDocument()
  })

  it('shows error when verifyPassword returns valid=false', async () => {
    mockCheckAuthStatus.mockResolvedValue({ valid: false })
    mockVerifyPassword.mockResolvedValue({ valid: false })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByLabelText('Access Code')).toBeInTheDocument()
    })

    const button = screen.getByRole('button', { name: 'Submit' })
    button.click()

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Incorrect access code')
    })
  })

  it('reveals app after successful login', async () => {
    mockCheckAuthStatus.mockResolvedValue({ valid: false })
    mockVerifyPassword.mockResolvedValue({ valid: true })
    render(<App />)

    await waitFor(() => {
      expect(screen.getByLabelText('Access Code')).toBeInTheDocument()
    })

    const button = screen.getByRole('button', { name: 'Submit' })
    button.click()

    await waitFor(() => {
      expect(screen.getByText('QA Agent Upload')).toBeInTheDocument()
    })
  })

  it('renders nothing during checking state', () => {
    // checkAuthStatus never resolves → stays in 'checking'
    mockCheckAuthStatus.mockReturnValue(new Promise(() => {}))
    const { container } = render(<App />)

    expect(container.innerHTML).toBe('')
  })
})
