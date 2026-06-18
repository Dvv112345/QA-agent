import type { FileWordCount } from '../types'

interface FinishedResultProps {
  mdResult: FileWordCount
  zipResults: FileWordCount[]
  totalWords: number | null
}

export default function FinishedResult({ mdResult, zipResults, totalWords }: FinishedResultProps) {
  return (
    <div className="results-container">
      <div className="results-section">
        <h4>Requirements Document</h4>
        <table className="results-table">
          <thead>
            <tr>
              <th>File</th>
              <th>Words</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{mdResult.file}</td>
              <td>{mdResult.words.toLocaleString()}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="results-section">
        <h4>Source Files</h4>
        <table className="results-table">
          <thead>
            <tr>
              <th>File</th>
              <th>Words</th>
            </tr>
          </thead>
          <tbody>
            {[...zipResults]
              .sort((a, b) => a.file.localeCompare(b.file))
              .map((f) => (
                <tr key={f.file}>
                  <td className="file-cell">{f.file}</td>
                  <td>{f.words.toLocaleString()}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>

      <div className="results-summary">
        <span>Total words</span>
        <span>{totalWords?.toLocaleString() ?? 0}</span>
      </div>
    </div>
  )
}
