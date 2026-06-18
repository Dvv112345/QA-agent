import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import FinishedResult from './FinishedResult'
import type { FileWordCount } from '../types'

describe('FinishedResult', () => {
  const mdResult: FileWordCount = { file: 'requirements.md', words: 150 }
  const zipResults: FileWordCount[] = [
    { file: 'src/main.py', words: 80 },
    { file: 'tests/test.py', words: 45 },
    { file: 'README.md', words: 200 },
  ]

  it('renders requirements document section with word count', () => {
    render(<FinishedResult mdResult={mdResult} zipResults={[]} totalWords={150} />)
    expect(screen.getByText('Requirements Document')).toBeInTheDocument()
    expect(screen.getByText('requirements.md')).toBeInTheDocument()
    expect(screen.getAllByText('150').length).toBeGreaterThanOrEqual(1)
  })

  it('renders source files table with sorted entries', () => {
    render(<FinishedResult mdResult={mdResult} zipResults={zipResults} totalWords={475} />)
    expect(screen.getByText('Source Files')).toBeInTheDocument()

    // Files should appear sorted alphabetically
    const fileNames = zipResults.map((r) => r.file).sort((a, b) => a.localeCompare(b))
    fileNames.forEach((name) => {
      expect(screen.getByText(name)).toBeInTheDocument()
    })
  })

  it('sorts zip results alphabetically by file name', () => {
    const unsorted: FileWordCount[] = [
      { file: 'z.py', words: 1 },
      { file: 'a.py', words: 2 },
      { file: 'm.py', words: 3 },
    ]
    render(<FinishedResult mdResult={mdResult} zipResults={unsorted} totalWords={156} />)

    // Get all table rows from the source files table
    const tables = screen.getAllByRole('table')
    // Source files table is the second one
    const sourceTable = tables[1]

    // Get all cell text in order from the source table
    const cellTexts = Array.from(sourceTable.querySelectorAll('tbody td.file-cell')).map(
      (td) => td.textContent,
    )
    expect(cellTexts).toEqual(['a.py', 'm.py', 'z.py'])
  })

  it('renders total words summary', () => {
    render(<FinishedResult mdResult={mdResult} zipResults={zipResults} totalWords={475} />)
    expect(screen.getByText('Total words')).toBeInTheDocument()
    expect(screen.getByText('475')).toBeInTheDocument()
  })

  it('shows 0 when totalWords is null', () => {
    render(<FinishedResult mdResult={mdResult} zipResults={zipResults} totalWords={null} />)
    expect(screen.getByText('Total words')).toBeInTheDocument()
    expect(screen.getByText('0')).toBeInTheDocument()
  })

  it('handles empty zip results array', () => {
    render(<FinishedResult mdResult={mdResult} zipResults={[]} totalWords={150} />)
    expect(screen.getByText('Source Files')).toBeInTheDocument()
    // No data rows in the source table — just header
    const tables = screen.getAllByRole('table')
    expect(tables[1].querySelectorAll('tbody tr').length).toBe(0)
    // 150 appears in both the requirements table and the total summary
    expect(screen.getAllByText('150').length).toBe(2)
  })

  it('formats large numbers with locale separators', () => {
    render(
      <FinishedResult
        mdResult={{ file: 'spec.md', words: 10000 }}
        zipResults={[{ file: 'big.py', words: 54321 }]}
        totalWords={64321}
      />,
    )
    expect(screen.getByText('10,000')).toBeInTheDocument()
    expect(screen.getByText('54,321')).toBeInTheDocument()
    expect(screen.getByText('64,321')).toBeInTheDocument()
  })
})
