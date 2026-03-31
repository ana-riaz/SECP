import { useState, useRef, useEffect, useCallback } from 'react'

const EXAMPLES = [
  'What are the most common action types?',
  'Summarize common violations in auditor-related adjudication orders',
]

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtSessionDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const now = new Date()
    const diffDays = Math.floor((now - d) / 86400000)
    if (diffDays === 0) return 'Today'
    if (diffDays === 1) return 'Yesterday'
    if (diffDays < 7)  return `${diffDays} days ago`
    return d.toLocaleDateString('en-PK', { day: 'numeric', month: 'short' })
  } catch { return '' }
}

// ── Copy button ───────────────────────────────────────────────────────────────

function CopyButton({ getText }) {
  const [copied, setCopied] = useState(false)
  function handleCopy() {
    navigator.clipboard.writeText(getText()).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }
  return (
    <button className="copy-btn" onClick={handleCopy} title="Copy response">
      {copied ? '✓ Copied' : '⎘ Copy'}
    </button>
  )
}

// ── Result helpers ─────────────────────────────────────────────────────────────

const SECP_ACT_URLS = {
  'Companies Act, 2017':
    'https://www.secp.gov.pk/enforcement/orders/orders-issued-under-companies-act-2017/',
  'Companies Rules, 1996':
    'https://www.secp.gov.pk/enforcement/orders/companies-rules-1996/',
  'Companies (General Provisions & Forms) Rules, 1985':
    'https://www.secp.gov.pk/enforcement/orders/companies-general-provisions-forms-rules-1985/',
  'Companies (Amendments) Ordinance, 2002':
    'https://www.secp.gov.pk/enforcement/orders/companies-amendments-ordinance-2002/',
  'Listed Companies Order, 2002':
    'https://www.secp.gov.pk/enforcement/orders/listed-companies-order-2002/',
}
const SECP_ORDERS_URL = 'https://www.secp.gov.pk/enforcement/orders/'

function resolveSourceUrl(result) {
  if (result.source_url) return result.source_url
  // Derive from acts — use first match found in the known mapping
  for (const act of (result.acts || [])) {
    const url = SECP_ACT_URLS[act]
    if (url) return url
  }
  // Fallback: check legal_provisions
  for (const p of (result.legal_provisions || [])) {
    const url = SECP_ACT_URLS[p.act || '']
    if (url) return url
  }
  return SECP_ORDERS_URL
}

function fmtDateLong(iso) {
  if (!iso) return ''
  try {
    return new Date(iso + 'T00:00:00').toLocaleDateString('en-PK', {
      day: 'numeric', month: 'long', year: 'numeric',
    })
  } catch { return iso }
}

function buildOrderTitle(r) {
  const parts = []
  const dateStr = fmtDateLong(r.order_date)
  parts.push(dateStr ? `Order Dated ${dateStr}` : 'Adjudication Order')

  const entities = (r.entity_names || []).map(e => e.replace(/^M\/s\.?\s*/i, '')).filter(Boolean)
  if (entities.length) parts.push(`against ${entities.join(', ')}`)

  const provs = r.legal_provisions || []
  if (provs.length) {
    // Group sections by act
    const actMap = new Map()
    for (const p of provs) {
      const act = p.act || ''
      if (p.section) actMap.set(act, [...(actMap.get(act) || []), p.section])
    }
    const provParts = []
    for (const [act, secs] of actMap) {
      const secStr = secs.map(s => `Section ${s}`).join(' read with ')
      provParts.push(act ? `${secStr} of the ${act}` : secStr)
    }
    if (provParts.length) parts.push(`under ${provParts.join('; ')}`)
  }

  return parts.join(' ')
}

// ── Copy text formatter ────────────────────────────────────────────────────────

function formatResultsText(results, query_info) {
  const lines = []
  lines.push(`SECP Adjudication Orders — "${query_info.original}"`)
  lines.push(`Found: ${results.length} order(s)`)
  lines.push('='.repeat(70))
  for (const r of results) {
    const title = buildOrderTitle(r)
    lines.push(`\n${r.rank}. ${title}`)
    if (r.order_reference) lines.push(`   Ref: ${r.order_reference}`)
    const meta = [
      r.penalty_display && r.penalty_display !== 'Not specified' ? r.penalty_display : null,
      r.issuing_officer || null,
    ].filter(Boolean).join('  ·  ')
    if (meta) lines.push(`   ${meta}`)
    if (r.violations?.length) lines.push(`   Violation: ${r.violations[0]}`)
    if (r.case_summary) lines.push(`   Summary: ${r.case_summary}`)
    lines.push(`   Source: ${resolveSourceUrl(r)}`)
  }
  return lines.join('\n')
}

// ── Single order list item ─────────────────────────────────────────────────────

function OrderListItem({ result }) {
  const title   = buildOrderTitle(result)
  const srcUrl  = resolveSourceUrl(result)
  const penalty = result.penalty_display && result.penalty_display !== 'Not specified'
    ? result.penalty_display : null

  return (
    <div className="order-list-item">
      <div className="oli-rank">{result.rank}</div>
      <div className="oli-body">
        <div className="oli-title">{title || result.filename}</div>
        <div className="oli-meta">
          {result.order_reference && (
            <span className="oli-ref">{result.order_reference}</span>
          )}
          {result.order_reference && (penalty || result.issuing_officer) && (
            <span className="oli-sep">·</span>
          )}
          {penalty && <span className="oli-penalty">{penalty}</span>}
          {penalty && result.issuing_officer && <span className="oli-sep">·</span>}
          {result.issuing_officer && (
            <span className="oli-officer">{result.issuing_officer.split(',')[0]}</span>
          )}
        </div>
        {result.violations?.length > 0 && (
          <div className="oli-violation">
            {result.violations[0]}
            {result.violations.length > 1 && (
              <span className="oli-vmore"> +{result.violations.length - 1} more</span>
            )}
          </div>
        )}
        {result.case_summary && (
          <div className="oli-summary">{result.case_summary}</div>
        )}
        <a href={srcUrl} target="_blank" rel="noopener noreferrer" className="oli-source">
          secp.gov.pk — Adjudication Orders ↗
        </a>
      </div>
    </div>
  )
}

// ── Narrative Summary View (summarize intent) ─────────────────────────────────

function NarrativeView({ narrative, results }) {
  if (!narrative) return null
  const { intro, themes, legal_provisions, penalty_summary, note } = narrative
  return (
    <div className="nv-wrap">
      {intro && <p className="nv-intro">{intro}</p>}

      {(themes || []).map((t, i) => (
        <div key={i} className="nv-theme">
          <div className="nv-theme-header">
            <span className="nv-theme-title">{i + 1}. {t.title}</span>
            {t.count > 0 && <span className="nv-theme-count">{t.count} case{t.count !== 1 ? 's' : ''}</span>}
          </div>
          <ul className="nv-bullets">
            {(t.bullets || []).map((b, j) => <li key={j}>{b}</li>)}
          </ul>
          {t.example_title && (
            <div className="nv-example">
              Example:{' '}
              {t.example_url
                ? <a href={t.example_url} target="_blank" rel="noopener noreferrer">{t.example_title} ↗</a>
                : <em>{t.example_title}</em>}
            </div>
          )}
        </div>
      ))}

      {(legal_provisions || []).length > 0 && (
        <div className="nv-section">
          <div className="nv-section-title">Legal Provisions Most Frequently Cited</div>
          <ul className="nv-bullets">
            {legal_provisions.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        </div>
      )}

      {penalty_summary && (
        <div className="nv-section">
          <div className="nv-section-title">Penalties Imposed</div>
          <p className="nv-penalty">{penalty_summary}</p>
        </div>
      )}

      {note && <p className="nv-note">{note}</p>}
    </div>
  )
}

// ── Lookup Detail View (single record) ────────────────────────────────────────

function LookupView({ result, allResults }) {
  if (!result) return null
  const title   = buildOrderTitle(result)
  const srcUrl  = resolveSourceUrl(result)
  const penalty = result.penalty_display && result.penalty_display !== 'Not specified'
    ? result.penalty_display : null
  const provs   = result.legal_provisions || []
  const secStr  = provs.length
    ? provs.map(p => `Section ${p.section} of the ${p.act}`).filter(Boolean).join('\n').trim()
    : null

  return (
    <div className="lv-wrap">
      <div className="lv-title">{title || result.filename}</div>
      <div className="lv-grid">
        {result.order_date    && <><span className="lv-label">Order Date</span><span className="lv-val">{fmtDateLong(result.order_date)}</span></>}
        {result.entity_names?.length > 0 && <><span className="lv-label">Entity</span><span className="lv-val">{result.entity_names.join(', ')}</span></>}
        {result.order_reference && <><span className="lv-label">Reference</span><span className="lv-val">{result.order_reference}</span></>}
        {secStr               && <><span className="lv-label">Section(s) Cited</span><span className="lv-val lv-val--pre">{secStr}</span></>}
        {penalty              && <><span className="lv-label">Penalty Imposed</span><span className="lv-val lv-val--penalty">{penalty}</span></>}
        {result.action_types?.length > 0 && <><span className="lv-label">Action Type</span><span className="lv-val">{result.action_types.join(', ')}</span></>}
        {result.issuing_officer && <><span className="lv-label">Issuing Officer</span><span className="lv-val">{result.issuing_officer}</span></>}
      </div>
      {result.case_summary && (
        <div className="lv-summary">
          <div className="lv-section-title">Summary</div>
          <p>{result.case_summary}</p>
        </div>
      )}
      {result.violations?.length > 0 && (
        <div className="lv-summary">
          <div className="lv-section-title">Violation</div>
          <p>{result.violations[0]}</p>
        </div>
      )}
      <a href={srcUrl} target="_blank" rel="noopener noreferrer" className="lv-source">
        Source: secp.gov.pk — Adjudication Orders ↗
      </a>
      {allResults?.length > 1 && (
        <div className="lv-more-hint">
          {allResults.length - 1} earlier order{allResults.length - 1 !== 1 ? 's' : ''} also found
        </div>
      )}
    </div>
  )
}

// ── Analytics Panel ───────────────────────────────────────────────────────────

function fmtPKR(n) {
  if (!n) return '—'
  if (n >= 1_000_000) return `PKR ${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000)     return `PKR ${(n / 1_000).toFixed(0)}K`
  return `PKR ${n.toLocaleString()}`
}

function AnalyticsTable({ title, rows, cols }) {
  if (!rows?.length) return null
  return (
    <div className="an-table-wrap">
      <div className="an-table-title">{title}</div>
      <table className="an-table">
        <thead>
          <tr>{cols.map(c => <th key={c.key}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map(c => <td key={c.key}>{c.fmt ? c.fmt(r[c.key], r) : (r[c.key] ?? '—')}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AnalyticsPanel({ analytics }) {
  if (!analytics) return null
  const { by_year, by_action_type, by_entity_category, by_legal_act,
          by_section, top_violations, by_category, penalty_summary, scope_total } = analytics

  return (
    <div className="an-panel">
      <div className="an-scope">
        Analysed <strong>{scope_total}</strong> adjudication order{scope_total !== 1 ? 's' : ''} in scope
      </div>

      {/* Penalty summary bar */}
      {penalty_summary?.cases_with_penalty > 0 && (
        <div className="an-summary-row">
          <div className="an-kpi">
            <span className="an-kpi-val">{penalty_summary.cases_with_penalty}</span>
            <span className="an-kpi-label">Orders with penalty</span>
          </div>
          <div className="an-kpi">
            <span className="an-kpi-val">{fmtPKR(penalty_summary.avg_pkr)}</span>
            <span className="an-kpi-label">Average penalty</span>
          </div>
          <div className="an-kpi">
            <span className="an-kpi-val">{fmtPKR(penalty_summary.max_pkr)}</span>
            <span className="an-kpi-label">Highest penalty</span>
          </div>
          <div className="an-kpi">
            <span className="an-kpi-val">{fmtPKR(penalty_summary.total_pkr)}</span>
            <span className="an-kpi-label">Total penalties imposed</span>
          </div>
        </div>
      )}

      <div className="an-tables-grid">
        <AnalyticsTable
          title="Orders by Year"
          rows={by_year}
          cols={[
            { key: 'year',  label: 'Year' },
            { key: 'count', label: 'Orders' },
            { key: 'penalty_cases', label: 'Penalty Orders' },
            { key: 'total_penalty_pkr', label: 'Total Penalties', fmt: fmtPKR },
          ]}
        />
        <AnalyticsTable
          title="Action Type Distribution"
          rows={by_action_type}
          cols={[
            { key: 'action_type', label: 'Action Type' },
            { key: 'count',       label: 'Count' },
          ]}
        />
        <AnalyticsTable
          title="Entity Category Breakdown"
          rows={by_entity_category}
          cols={[
            { key: 'entity_category', label: 'Entity Category' },
            { key: 'count',           label: 'Count' },
          ]}
        />
        <AnalyticsTable
          title="Most Cited Legal Acts"
          rows={by_legal_act}
          cols={[
            { key: 'act',   label: 'Act / Regulation' },
            { key: 'count', label: 'Citations' },
          ]}
        />
        <AnalyticsTable
          title="Most Cited Sections"
          rows={by_section}
          cols={[
            { key: 'section', label: 'Section' },
            { key: 'act',     label: 'Act' },
            { key: 'count',   label: 'Count' },
          ]}
        />
        <AnalyticsTable
          title="Common Violation Themes"
          rows={top_violations}
          cols={[
            { key: 'violation', label: 'Violation' },
            { key: 'count',     label: 'Count' },
          ]}
        />
        <AnalyticsTable
          title="Distribution by Legal Framework"
          rows={by_category}
          cols={[
            { key: 'category', label: 'Legal Framework' },
            { key: 'count',    label: 'Orders' },
          ]}
        />
      </div>
    </div>
  )
}

// ── Message components ────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div className="msg-row system">
      <div className="avatar system">S</div>
      <div className="bubble system">
        <div className="typing"><span /><span /><span /></div>
      </div>
    </div>
  )
}

function SystemMessage({ msg }) {
  if (msg.type === 'error') {
    return (
      <div className="msg-row system">
        <div className="avatar system">S</div>
        <div className="bubble system">
          <p className="error-msg">Error: {msg.text}</p>
        </div>
      </div>
    )
  }

  if (msg.type === 'refusal') {
    return (
      <div className="msg-row system">
        <div className="avatar system">S</div>
        <div className="bubble system">
          <div className="refusal-msg">
            <span className="refusal-icon">⚠</span>
            {msg.text}
          </div>
          <CopyButton getText={() => msg.text} />
        </div>
      </div>
    )
  }

  if (msg.type === 'results') {
    const { results, query_info, analytics, narrative } = msg.data
    const intent   = query_info.intent || 'browse'
    const cat      = query_info.entity_category
    const sections = query_info.sections || []
    const acts     = query_info.acts || []

    function buildHeader() {
      const n = results.length
      const orderWord = `${n} adjudication order${n !== 1 ? 's' : ''}`
      if (intent === 'summarize') {
        return <>Summary: <strong>{query_info.original}</strong></>
      }
      if (intent === 'stats') {
        return <>Trend Analysis: <strong>{query_info.original}</strong></>
      }
      if (intent === 'lookup') {
        return <>Most Recent Result for <strong>&ldquo;{query_info.original}&rdquo;</strong></>
      }
      if (cat) {
        const catLabel = {
          'Listed Company':           'listed companies',
          'Unlisted Company':         'unlisted companies',
          'Broker':                   'brokers',
          'Asset Management Company': 'asset management companies',
          'NBFC':                     'NBFCs',
          'Insurance Company':        'insurance companies',
        }[cat] || cat
        return <>Found <strong>{orderWord}</strong> against <strong>{catLabel}</strong></>
      }
      if (acts.length && sections.length) {
        return <>Found <strong>{orderWord}</strong> under <strong>Section {sections.join(', ')}</strong> of the <strong>{acts[0]}</strong></>
      }
      if (sections.length) {
        return <>Found <strong>{orderWord}</strong> citing <strong>Section {sections.join(', ')}</strong></>
      }
      if (acts.length) {
        return <>Found <strong>{orderWord}</strong> under the <strong>{acts[0]}</strong></>
      }
      return <>Found <strong>{orderWord}</strong> for <strong>&ldquo;{query_info.original}&rdquo;</strong></>
    }

    function renderBody() {
      if (results.length === 0 && !analytics && !narrative) {
        return (
          <p style={{ color: 'var(--text-muted)', fontSize: 13, marginTop: 8 }}>
            No matching orders found. Try a broader query or different keywords.
          </p>
        )
      }
      // Summarize: narrative first, then full list collapsed
      if (intent === 'summarize') {
        return (
          <>
            {narrative
              ? <NarrativeView narrative={narrative} />
              : results.length > 0 && (
                <div className="order-list">
                  {results.map((r, i) => <OrderListItem key={i} result={r} />)}
                </div>
              )
            }
          </>
        )
      }
      // Stats: analytics tables, no list
      if (intent === 'stats') {
        return <AnalyticsPanel analytics={analytics} />
      }
      // Lookup: single detailed card, then mention of more
      if (intent === 'lookup') {
        return <LookupView result={results[0]} allResults={results} />
      }
      // Browse: regular list
      return (
        <div className="order-list">
          {results.map((r, i) => <OrderListItem key={i} result={r} />)}
        </div>
      )
    }

    return (
      <div className="msg-row system">
        <div className="avatar system">S</div>
        <div className="bubble system">
          <div className="results-header">{buildHeader()}</div>
          {renderBody()}
          <CopyButton getText={() => formatResultsText(results, query_info)} />
        </div>
      </div>
    )
  }

  return null
}

// ── Chat History Panel ────────────────────────────────────────────────────────

function HistoryPanel({ sessions, currentId, onSelect, onNew, onDelete, onRename }) {
  const [hoverId,    setHoverId]    = useState(null)
  const [editingId,  setEditingId]  = useState(null)
  const [editTitle,  setEditTitle]  = useState('')
  const editRef = useRef(null)

  function startRename(e, s) {
    e.stopPropagation()
    setEditingId(s.session_id)
    setEditTitle(s.title || '')
    setTimeout(() => { editRef.current?.select() }, 0)
  }

  function commitRename(sessionId) {
    const title = editTitle.trim()
    if (title) onRename(sessionId, title)
    setEditingId(null)
  }

  function onEditKey(e, sessionId) {
    if (e.key === 'Enter')  { e.preventDefault(); commitRename(sessionId) }
    if (e.key === 'Escape') setEditingId(null)
  }

  return (
    <aside className="history-panel">
      <div className="history-header">
        <span className="history-title">Chats</span>
        <button className="history-new-btn" onClick={onNew} title="New chat">+</button>
      </div>
      <div className="history-list">
        {sessions.length === 0 ? (
          <div className="history-empty">No chat history yet</div>
        ) : (
          sessions.map(s => (
            <div
              key={s.session_id}
              className={`history-item ${s.session_id === currentId ? 'active' : ''}`}
              onClick={() => editingId !== s.session_id && onSelect(s.session_id)}
              onMouseEnter={() => setHoverId(s.session_id)}
              onMouseLeave={() => setHoverId(null)}
            >
              {editingId === s.session_id ? (
                <input
                  ref={editRef}
                  className="history-rename-input"
                  value={editTitle}
                  onChange={e => setEditTitle(e.target.value)}
                  onBlur={() => commitRename(s.session_id)}
                  onKeyDown={e => onEditKey(e, s.session_id)}
                  onClick={e => e.stopPropagation()}
                  maxLength={120}
                />
              ) : (
                <>
                  <div className="history-item-title">{s.title || 'Untitled'}</div>
                  <div className="history-item-meta">
                    {fmtSessionDate(s.updated_at)}
                    {s.message_count > 0 && <span> · {s.message_count} msg{s.message_count !== 1 ? 's' : ''}</span>}
                  </div>
                  {hoverId === s.session_id && (
                    <div className="history-item-actions">
                      <button
                        className="history-action-btn"
                        title="Rename"
                        onClick={e => startRename(e, s)}
                      >✎</button>
                      <button
                        className="history-action-btn history-action-btn--delete"
                        title="Delete"
                        onClick={e => { e.stopPropagation(); onDelete(s.session_id) }}
                      >🗑</button>
                    </div>
                  )}
                </>
              )}
            </div>
          ))
        )}
      </div>
    </aside>
  )
}

// ── Main Chat Component ───────────────────────────────────────────────────────

export default function Chat({ token }) {
  const authHeaders = { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }

  const [messages,   setMessages]   = useState([])
  const [input,      setInput]      = useState('')
  const [loading,    setLoading]    = useState(false)
  const [sessions,   setSessions]   = useState([])
  const [currentId,  setCurrentId]  = useState(null)   // active session_id

  const bottomRef   = useRef(null)
  const textareaRef = useRef(null)
  // Ref to track current session_id inside async callbacks without stale closure
  const currentIdRef = useRef(null)
  currentIdRef.current = currentId

  // Load session list on mount
  useEffect(() => {
    fetch('/api/chat/sessions', { headers: authHeaders })
      .then(r => r.json())
      .then(data => setSessions(Array.isArray(data) ? data : []))
      .catch(() => {})
  }, [])

  // Auto-scroll
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  // Persist messages to MongoDB after each exchange
  const persist = useCallback(async (msgs, sessionId, firstQuery) => {
    try {
      if (!sessionId) {
        // Create new session
        const title = firstQuery?.slice(0, 100) || 'New Chat'
        const res = await fetch('/api/chat/sessions', {
          method:  'POST',
          headers: authHeaders,
          body:    JSON.stringify({ title, messages: msgs }),
        })
        const data = await res.json()
        const newId = data.session_id
        setCurrentId(newId)
        currentIdRef.current = newId
        setSessions(prev => [{
          session_id:    newId,
          title,
          message_count: msgs.length,
          updated_at:    new Date().toISOString(),
        }, ...prev])
        return newId
      } else {
        // Update existing session
        await fetch(`/api/chat/sessions/${sessionId}`, {
          method:  'PUT',
          headers: authHeaders,
          body:    JSON.stringify({ messages: msgs }),
        })
        setSessions(prev => prev.map(s =>
          s.session_id === sessionId
            ? { ...s, message_count: msgs.length, updated_at: new Date().toISOString() }
            : s
        ))
        return sessionId
      }
    } catch { return sessionId }
  }, [])

  async function sendQuery(q) {
    const query = (q || input).trim()
    if (!query || loading) return

    setInput('')
    const userMsg = { type: 'user', text: query }
    const newMsgs = [...messages, userMsg]
    setMessages(newMsgs)
    setLoading(true)

    const isFirst = messages.length === 0

    try {
      const res = await fetch('/api/search', {
        method:  'POST',
        headers: authHeaders,
        body:    JSON.stringify({ query, top_k: 5, use_llm: true }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()

      let sysMsg
      if (data.refusal) {
        sysMsg = { type: 'refusal', text: data.message }
      } else {
        sysMsg = { type: 'results', data }
      }

      const finalMsgs = [...newMsgs, sysMsg]
      setMessages(finalMsgs)

      // Save to MongoDB
      await persist(finalMsgs, currentIdRef.current, isFirst ? query : null)
    } catch (err) {
      const finalMsgs = [...newMsgs, { type: 'error', text: err.message }]
      setMessages(finalMsgs)
      await persist(finalMsgs, currentIdRef.current, isFirst ? query : null)
    } finally {
      setLoading(false)
    }
  }

  async function loadSession(sessionId) {
    try {
      const res  = await fetch(`/api/chat/sessions/${sessionId}`, { headers: authHeaders })
      const data = await res.json()
      setMessages(data.messages || [])
      setCurrentId(sessionId)
    } catch {}
  }

  function startNewChat() {
    setMessages([])
    setCurrentId(null)
    setInput('')
    textareaRef.current?.focus()
  }

  async function deleteSession(sessionId) {
    try {
      await fetch(`/api/chat/sessions/${sessionId}`, { method: 'DELETE', headers: authHeaders })
      setSessions(prev => prev.filter(s => s.session_id !== sessionId))
      if (currentId === sessionId) startNewChat()
    } catch {}
  }

  async function renameSession(sessionId, newTitle) {
    try {
      await fetch(`/api/chat/sessions/${sessionId}/rename`, {
        method:  'PATCH',
        headers: authHeaders,
        body:    JSON.stringify({ title: newTitle }),
      })
      setSessions(prev => prev.map(s =>
        s.session_id === sessionId ? { ...s, title: newTitle } : s
      ))
    } catch {}
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendQuery()
    }
  }

  return (
    <div className="chat-page">
      <HistoryPanel
        sessions={sessions}
        currentId={currentId}
        onSelect={loadSession}
        onNew={startNewChat}
        onDelete={deleteSession}
        onRename={renameSession}
      />

      <div className="chat-area">
        <div className="messages">
          {messages.length === 0 && !loading && (
            <div className="welcome">
              <h3>Adjudication Research Assistant</h3>
              <p>
                Search SECP adjudication orders using natural language. Ask about
                violations, penalties, companies, legal provisions, and more.
              </p>
              <div className="example-queries">
                {EXAMPLES.map((q, i) => (
                  <button key={i} className="example-q" onClick={() => sendQuery(q)}>
                    {q}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            msg.type === 'user'
              ? (
                <div key={i} className="msg-row user">
                  <div className="bubble user">{msg.text}</div>
                </div>
              )
              : <SystemMessage key={i} msg={msg} />
          ))}

          {loading && <TypingIndicator />}
          <div ref={bottomRef} />
        </div>

        <div className="input-bar">
          <textarea
            ref={textareaRef}
            rows={1}
            placeholder="Type your query here. Ask about adjudication orders… (Enter to send, Shift+Enter for new line)"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKey}
            disabled={loading}
            style={{ minHeight: 46 }}
          />
          <button
            className="send-btn"
            onClick={() => sendQuery()}
            disabled={loading || !input.trim()}
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
        </div>
      </div>
    </div>
  )
}
