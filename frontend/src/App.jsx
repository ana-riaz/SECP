import { useState, useEffect } from 'react'
import Chat          from './components/Chat'
import PDFSummarizer from './components/PDFSummarizer'
import Login         from './components/Login'

export default function App() {
  const [tab,   setTab]   = useState('search')
  const [token, setToken] = useState(() => localStorage.getItem('secp_token') || '')
  const [checking, setChecking] = useState(true)

  // Validate stored token on mount
  useEffect(() => {
    if (!token) { setChecking(false); return }
    fetch('/api/auth/me', {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => { if (!r.ok) throw new Error('invalid') })
      .catch(() => { localStorage.removeItem('secp_token'); setToken('') })
      .finally(() => setChecking(false))
  }, [])

  function handleLogin(newToken) {
    setToken(newToken)
  }

  function handleLogout() {
    localStorage.removeItem('secp_token')
    setToken('')
  }

  if (checking) return null   // brief flash while validating stored token

  if (!token) return <Login onLogin={handleLogin} />

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
        <button className="logout-btn" onClick={handleLogout} title="Sign out">
          Sign Out
        </button>
      </header>

      <div className="page-content">
        {tab === 'search'    && <Chat token={token} />}
        {tab === 'summarize' && <PDFSummarizer token={token} />}
      </div>
    </div>
  )
}
