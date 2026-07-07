import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import LoginModal from './LoginModal'

function makeProps(
  overrides: Partial<{
    onLogin: () => Promise<void>
    error: string | null
    loading: boolean
  }> = {},
) {
  return {
    onLogin: vi.fn().mockResolvedValue(undefined),
    error: null as string | null,
    loading: false,
    ...overrides,
  }
}

describe('LoginModal', () => {
  it('renders modal overlay with password input and submit button', () => {
    render(<LoginModal {...makeProps()} />)

    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText('Access Code')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()
    expect(screen.getByText('QA Agent')).toBeInTheDocument()
  })

  it('has no close button in the DOM', () => {
    render(<LoginModal {...makeProps()} />)

    expect(screen.queryByRole('button', { name: /close/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/close/i)).not.toBeInTheDocument()
  })

  it('auto-focuses the password input on mount', () => {
    render(<LoginModal {...makeProps()} />)

    const input = screen.getByLabelText('Access Code')
    expect(document.activeElement).toBe(input)
  })

  it('calls onLogin with entered password when submit button is clicked', () => {
    const onLogin = vi.fn().mockResolvedValue(undefined)
    render(<LoginModal {...makeProps({ onLogin })} />)

    const input = screen.getByLabelText('Access Code')
    fireEvent.change(input, { target: { value: 'secret123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    expect(onLogin).toHaveBeenCalledTimes(1)
    expect(onLogin).toHaveBeenCalledWith('secret123')
  })

  it('calls onLogin when form is submitted via Enter key', () => {
    const onLogin = vi.fn().mockResolvedValue(undefined)
    render(<LoginModal {...makeProps({ onLogin })} />)

    const input = screen.getByLabelText('Access Code')
    fireEvent.change(input, { target: { value: 'mypass' } })
    fireEvent.submit(screen.getByRole('dialog').querySelector('form')!)

    expect(onLogin).toHaveBeenCalledTimes(1)
    expect(onLogin).toHaveBeenCalledWith('mypass')
  })

  it('shows error message when error prop is set', () => {
    render(<LoginModal {...makeProps({ error: 'Incorrect access code' })} />)

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Incorrect access code')
  })

  it('does not show error when error prop is null', () => {
    render(<LoginModal {...makeProps({ error: null })} />)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('submit button shows "Verifying…" and is disabled when loading', () => {
    render(<LoginModal {...makeProps({ loading: true })} />)

    const button = screen.getByRole('button', { name: 'Verifying…' })
    expect(button).toBeDisabled()
  })

  it('aria-modal and role are set on overlay', () => {
    render(<LoginModal {...makeProps()} />)

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
  })

  it('does not call onLogin when loading and submit is clicked', () => {
    const onLogin = vi.fn().mockResolvedValue(undefined)
    render(<LoginModal {...makeProps({ onLogin, loading: true })} />)

    fireEvent.click(screen.getByRole('button', { name: 'Verifying…' }))
    expect(onLogin).not.toHaveBeenCalled()
  })
})
