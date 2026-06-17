import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import HomePage from './pages/HomePage'
import LoadingPage from './pages/LoadingPage'

describe('Route → page mapping', () => {
  it('renders HomePage at route /', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/loading" element={<LoadingPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await screen.findByText('QA Agent Upload')
    expect(screen.getByText(/upload & analyze/i)).toBeInTheDocument()
  })

  it('renders LoadingPage at route /loading', async () => {
    render(
      <MemoryRouter initialEntries={['/loading']}>
        <Routes>
          <Route path="/" element={<HomePage />} />
          <Route path="/loading" element={<LoadingPage />} />
        </Routes>
      </MemoryRouter>,
    )

    await screen.findByText('← Back')
    expect(screen.getByText(/no files to upload/i)).toBeInTheDocument()
  })
})
