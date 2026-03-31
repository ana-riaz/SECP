import { useState } from 'react'
import Chat          from './components/Chat'
import PDFSummarizer from './components/PDFSummarizer'

export default function App() {
  const [tab, setTab] = useState('search')

  return (
    <div className="app">
      <header className="header">
        <span className="header-title">SECP Adjudication Research Assistant</span>
        <div className="tabs">
          <button
            className={`tab ${tab === 'search' ? 'active' : ''}`}
            onClick={() => setTab('search')}
          >
            AI Chat Assistant
          </button>
          <button
            className={`tab ${tab === 'summarize' ? 'active' : ''}`}
            onClick={() => setTab('summarize')}
          >
            AI Search 
          </button>
        </div>
      </header>

      <div className="page-content">
        {tab === 'search'    && <Chat />}
        {tab === 'summarize' && <PDFSummarizer />}
      </div>
    </div>
  )
}