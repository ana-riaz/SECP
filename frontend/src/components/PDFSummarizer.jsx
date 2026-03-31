import { useState, useEffect, useRef } from 'react'
import {
  Search, X, FileText, Upload, CheckCircle, Filter,
  Calendar, Hash, User, Scale, AlertTriangle, Download,
  ClipboardList, BookOpen, Gavel, ChevronDown,
} from 'lucide-react'

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso + 'T00:00:00').toLocaleDateString('en-PK', {
      day: 'numeric', month: 'short', year: 'numeric'
    })
  } catch { return iso }
}

function fmtPenalty(pkr) {
  if (!pkr || pkr <= 0) return null
  if (pkr >= 1_000_000) return `PKR ${(pkr / 1_000_000).toFixed(1)}M`
  if (pkr >= 1_000)     return `PKR ${(pkr / 1_000).toFixed(0)}K`
  return `PKR ${Number(pkr).toLocaleString()}`
}

function ActionBadge({ type }) {
  const cls = {
    'Penalty':              'badge-penalty',
    'Warning':              'badge-warning',
    'Show Cause Notice':    'badge-scn',
    'Compliance Direction': 'badge-direction',
    'Settlement':           'badge-settlement',
  }[type] || 'badge-other'
  return <span className={`action-badge ${cls}`}>{type}</span>
}

// ─── Full Summary Modal ───────────────────────────────────────────────────────

function SummaryModal({ docId, filename, onClose, token }) {
  const [summary, setSummary] = useState(null)
  const [loading, setLoading] = useState(true)
  const authH = token ? { Authorization: `Bearer ${token}` } : {}

  useEffect(() => {
    fetch(`/api/summaries/${docId}`, { headers: authH })
      .then(r => r.json())
      .then(d => { setSummary(d.summary); setLoading(false) })
      .catch(() => setLoading(false))
  }, [docId])

  // Close on backdrop click
  function onBackdrop(e) { if (e.target === e.currentTarget) onClose() }

  // Close on Escape
  useEffect(() => {
    function onKey(e) { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  function downloadText() {
    if (!summary) return
    const hdr = summary.case_header || {}
    const lines = [
      'SECP ADJUDICATION ORDER — STRUCTURED SUMMARY',
      `Source: ${filename}`,
      '',
      '═══════════════════════════════════════════════════════════════════',
      'CASE HEADER',
      '═══════════════════════════════════════════════════════════════════',
      `Reference        : ${hdr.order_reference || '—'}`,
      `Respondent(s)    : ${hdr.respondent || '—'}`,
      `Order Date       : ${hdr.order_date || '—'}`,
      `Law(s) Applied   : ${hdr.laws_applied || '—'}`,
      `Issuing Authority: ${hdr.issuing_authority || '—'}`,
      '',
      ...['case_background','violation_identified','legal_provisions_applied',
          'secp_determination','penalty_or_sanction','source_citation','current_status']
        .flatMap(k => [
          `═══ ${k.replace(/_/g,' ').toUpperCase()} ═══`,
          Array.isArray(summary[k]) ? summary[k].join('\n') : (summary[k] || '—'),
          '',
        ]),
      'KEY FACTS',
      ...((summary.key_facts || []).map((f,i) => `  ${i+1}. ${f}`)),
    ]
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = filename.replace('.pdf','_summary.txt'); a.click()
  }

  const sections = [
    { title: 'Case Background',          key: 'case_background'          },
    { title: 'Key Facts',                key: 'key_facts',   isList: true },
    { title: 'Violation Identified',     key: 'violation_identified'     },
    { title: 'Legal Provisions Applied', key: 'legal_provisions_applied' },
    { title: "SECP's Determination",     key: 'secp_determination'       },
    { title: 'Penalty / Sanction',       key: 'penalty_or_sanction'      },
    { title: 'Source Citation',          key: 'source_citation', isPre: true },
    { title: 'Current Status',           key: 'current_status'           },
  ]

  return (
    <div className="modal-backdrop" onClick={onBackdrop}>
      <div className="modal-box">
        {/* Modal header */}
        <div className="modal-header">
          <div className="modal-header-left">
            {summary ? (
              <>
                <h2>{(summary.case_header?.respondent) || filename}</h2>
                <div className="modal-meta-row">
                  {summary.case_header?.order_reference && (
                    <span>{summary.case_header.order_reference}</span>
                  )}
                  {summary.case_header?.order_date && (
                    <span>{summary.case_header.order_date}</span>
                  )}
                  {summary.case_header?.laws_applied && (
                    <span>{summary.case_header.laws_applied}</span>
                  )}
                </div>
              </>
            ) : (
              <h2>{filename}</h2>
            )}
          </div>
          <div className="modal-header-right">
            {summary && (
              <button className="action-btn" onClick={downloadText}><Download size={13} /> Download .txt</button>
            )}
            <button className="modal-close-btn" onClick={onClose}><X size={15} /></button>
          </div>
        </div>

        {/* Modal body */}
        <div className="modal-body">
          {loading ? (
            <div className="modal-loading">Loading summary…</div>
          ) : !summary ? (
            <div className="modal-loading">Summary not available.</div>
          ) : (
            <div className="modal-sections">
              {sections.map(sec => {
                const val = summary[sec.key]
                const empty = !val || val === 'Not available in this order.'
                  || val === 'Not described in this order.'
                  || val === 'No subsequent status recorded in this order.'
                return (
                  <div key={sec.key} className="modal-section">
                    <div className="modal-section-title">{sec.title}</div>
                    <div className="modal-section-body">
                      {empty ? (
                        <p className="sum-empty">Not recorded in this order.</p>
                      ) : sec.isList ? (
                        <ol className="sum-list">
                          {(Array.isArray(val) ? val : [val]).map((it,i) => <li key={i}>{it}</li>)}
                        </ol>
                      ) : sec.isPre ? (
                        <pre className="sum-pre">{val}</pre>
                      ) : (
                        <p>{val}</p>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Filter Panel ─────────────────────────────────────────────────────────────

const ACTION_OPTIONS = ['Penalty', 'Warning', 'Show Cause Notice', 'Compliance Direction', 'Settlement', 'Other']

function FilterPanel({ cards, filters, onChange, onClear, count }) {
  const categories = [...new Set(cards.map(c => c.entity_category).filter(Boolean))].sort()
  const sectors    = [...new Set(cards.map(c => c.sector).filter(Boolean))].sort()
  const acts       = [...new Set(cards.flatMap(c => c.acts || []).filter(Boolean))].sort()

  function set(field, val) { onChange({ ...filters, [field]: val }) }
  function toggleAction(a) {
    const cur = filters.action_types || []
    set('action_types', cur.includes(a) ? cur.filter(x => x !== a) : [...cur, a])
  }

  const hasFilters = Object.values(filters).some(v =>
    Array.isArray(v) ? v.length > 0 : !!v
  )

  return (
    <aside className="filter-panel">
      <div className="filter-panel-header">
        <span className="filter-panel-title"><Filter size={13} /> Filters</span>
        <span className="filter-result-count">{count} result{count !== 1 ? 's' : ''}</span>
        {hasFilters && (
          <button className="filter-clear-all" onClick={onClear}>Clear all</button>
        )}
      </div>

      <div className="filter-panel-body">

        {/* Date Range */}
        <div className="filter-group">
          <div className="filter-group-label">Date Range</div>
          <input type="date" className="filter-input" value={filters.date_from || ''}
            onChange={e => set('date_from', e.target.value)} placeholder="From" />
          <input type="date" className="filter-input" style={{marginTop:6}} value={filters.date_to || ''}
            onChange={e => set('date_to', e.target.value)} placeholder="To" />
        </div>

        {/* Entity Name */}
        <div className="filter-group">
          <div className="filter-group-label">Entity Name</div>
          <input className="filter-input" placeholder="Company or individual name…"
            value={filters.entity_name || ''}
            onChange={e => set('entity_name', e.target.value)} />
        </div>

        {/* Entity Category */}
        <div className="filter-group">
          <div className="filter-group-label">Entity Category</div>
          <select className="filter-select" value={filters.entity_category || ''}
            onChange={e => set('entity_category', e.target.value)}>
            <option value="">All categories</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>

        {/* Sector */}
        <div className="filter-group">
          <div className="filter-group-label">Sector</div>
          <select className="filter-select" value={filters.sector || ''}
            onChange={e => set('sector', e.target.value)}>
            <option value="">All sectors</option>
            {sectors.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>

        {/* Violation text */}
        <div className="filter-group">
          <div className="filter-group-label">Violation Description</div>
          <input className="filter-input" placeholder="Search violations…"
            value={filters.violation_text || ''}
            onChange={e => set('violation_text', e.target.value)} />
        </div>

        {/* Legal Provisions */}
        <div className="filter-group">
          <div className="filter-group-label">Legal Provisions</div>
          <input className="filter-input" placeholder="Section number (e.g. 510)"
            value={filters.section || ''}
            onChange={e => set('section', e.target.value)} />
          <select className="filter-select" style={{marginTop:6}} value={filters.act || ''}
            onChange={e => set('act', e.target.value)}>
            <option value="">All acts</option>
            {acts.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </div>

        {/* Penalty Range */}
        <div className="filter-group">
          <div className="filter-group-label">Penalty Range (PKR)</div>
          <div className="filter-range-row">
            <input className="filter-input" type="number" placeholder="Min"
              value={filters.penalty_min || ''}
              onChange={e => set('penalty_min', e.target.value)} />
            <span className="filter-range-sep">–</span>
            <input className="filter-input" type="number" placeholder="Max"
              value={filters.penalty_max || ''}
              onChange={e => set('penalty_max', e.target.value)} />
          </div>
        </div>

        {/* Action Type */}
        <div className="filter-group">
          <div className="filter-group-label">Action Type</div>
          <div className="filter-checkboxes">
            {ACTION_OPTIONS.map(a => (
              <label key={a} className="filter-checkbox-label">
                <input type="checkbox"
                  checked={(filters.action_types || []).includes(a)}
                  onChange={() => toggleAction(a)} />
                <span>{a}</span>
              </label>
            ))}
          </div>
        </div>

        {/* Issuing Officer */}
        <div className="filter-group">
          <div className="filter-group-label">Issuing Officer</div>
          <input className="filter-input" placeholder="Officer name…"
            value={filters.issuing_officer || ''}
            onChange={e => set('issuing_officer', e.target.value)} />
        </div>

        {/* Order Reference */}
        <div className="filter-group">
          <div className="filter-group-label">Order Reference</div>
          <input className="filter-input" placeholder="Reference number…"
            value={filters.order_reference || ''}
            onChange={e => set('order_reference', e.target.value)} />
        </div>

      </div>
    </aside>
  )
}

// ─── Summary Library ──────────────────────────────────────────────────────────

const EMPTY_FILTERS = {
  date_from: '', date_to: '', entity_name: '', entity_category: '',
  sector: '', violation_text: '', section: '', act: '',
  penalty_min: '', penalty_max: '', action_types: [],
  issuing_officer: '', order_reference: '',
}

function applyFilters(cards, filters, globalQ) {
  return cards.filter(c => {
    const q = globalQ.toLowerCase().trim()
    if (q) {
      const hay = [
        c.entity, c.order_reference, c.issuing_officer,
        ...(c.violations || []), ...(c.sections || []), ...(c.acts || []),
      ].join(' ').toLowerCase()
      if (!hay.includes(q)) return false
    }
    if (filters.date_from && c.order_date && c.order_date < filters.date_from) return false
    if (filters.date_to   && c.order_date && c.order_date > filters.date_to)   return false
    if (filters.entity_name) {
      if (!c.entity.toLowerCase().includes(filters.entity_name.toLowerCase())) return false
    }
    if (filters.entity_category && c.entity_category !== filters.entity_category) return false
    if (filters.sector           && c.sector          !== filters.sector)          return false
    if (filters.violation_text) {
      const vt = filters.violation_text.toLowerCase()
      if (!(c.violations || []).some(v => v.toLowerCase().includes(vt))) return false
    }
    if (filters.section) {
      const sec = filters.section.toLowerCase()
      if (!(c.sections || []).some(s => s.toLowerCase().includes(sec))) return false
    }
    if (filters.act) {
      if (!(c.acts || []).some(a => a === filters.act)) return false
    }
    if (filters.penalty_min && Number(filters.penalty_min) > 0) {
      if (!c.penalty_pkr || c.penalty_pkr < Number(filters.penalty_min)) return false
    }
    if (filters.penalty_max && Number(filters.penalty_max) > 0) {
      if (!c.penalty_pkr || c.penalty_pkr > Number(filters.penalty_max)) return false
    }
    if ((filters.action_types || []).length > 0) {
      if (!filters.action_types.some(a => (c.action_types || []).includes(a))) return false
    }
    if (filters.issuing_officer) {
      if (!(c.issuing_officer || '').toLowerCase().includes(filters.issuing_officer.toLowerCase())) return false
    }
    if (filters.order_reference) {
      if (!(c.order_reference || '').toLowerCase().includes(filters.order_reference.toLowerCase())) return false
    }
    return true
  })
}

function SummaryLibrary({ token }) {
  const authH = token ? { Authorization: `Bearer ${token}` } : {}
  const [cards,     setCards]    = useState([])
  const [loading,   setLoading]  = useState(true)
  const [globalQ,   setGlobalQ]  = useState('')
  const [filters,   setFilters]  = useState(EMPTY_FILTERS)
  const [modalId,   setModalId]  = useState(null)
  const [modalFile, setModalFile] = useState('')
  const [selected,  setSelected] = useState(new Set())
  const [consolidated, setConsolidated] = useState(null)
  const [consolidating, setConsolidating] = useState(false)
  const [conError,  setConError] = useState(null)
  const [scope,     setScope]    = useState('')
  const [showConPanel, setShowConPanel] = useState(false)
  const [generatingId, setGeneratingId] = useState(null)

  useEffect(() => {
    fetch('/api/summaries', { headers: authH })
      .then(r => r.json())
      .then(data => { setCards(Array.isArray(data) ? data : []); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  const filtered = applyFilters(cards, filters, globalQ)

  function openModal(card) {
    if (generatingId) return
    setGeneratingId(card.doc_id)
    setTimeout(() => {
      setGeneratingId(null)
      setModalId(card.doc_id)
      setModalFile(card.filename)
    }, 2000)
  }

  function toggleSelect(e, docId) {
    e.stopPropagation()
    setSelected(prev => {
      const next = new Set(prev)
      next.has(docId) ? next.delete(docId) : next.add(docId)
      return next
    })
  }

  async function generateConsolidated() {
    setConsolidating(true); setConError(null)
    try {
      const res = await fetch('/api/summaries/consolidated', {
        method: 'POST', headers: { 'Content-Type': 'application/json', ...authH },
        body: JSON.stringify({ doc_ids: [...selected], scope }),
      })
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`)
      const data = await res.json()
      setConsolidated(data)
      setShowConPanel(false)
    } catch (err) { setConError(err.message) }
    finally { setConsolidating(false) }
  }

  return (
    <div className="lib-page">

      {/* ── Search bar ── */}
      <div className="lib-searchbar-row">
        <div className="lib-searchbar-wrap">
          <span className="lib-search-icon"><Search size={15} /></span>
          <input
            className="lib-searchbar"
            placeholder="Search orders by entity name, violation, reference, officer, section…"
            value={globalQ}
            onChange={e => setGlobalQ(e.target.value)}
          />
          {globalQ && (
            <button className="lib-search-clear" onClick={() => setGlobalQ('')}><X size={13} /></button>
          )}
        </div>
        {selected.size >= 2 && (
          <button className="action-btn primary lib-report-btn"
            onClick={() => setShowConPanel(true)}>
            <ClipboardList size={14} />
            Generate Report ({selected.size})
          </button>
        )}
      </div>

      {/* ── Main area: cards + filter panel ── */}
      <div className="lib-main">

        {/* Cards area */}
        <div className="lib-cards-area">
          {loading ? (
            <div className="lib-status">Loading summaries…</div>
          ) : filtered.length === 0 ? (
            <div className="lib-status">No orders match the current filters.</div>
          ) : (
            filtered.map(card => (
              <div
                key={card.doc_id}
                className={`order-card ${selected.has(card.doc_id) ? 'order-card-selected' : ''}`}
                onClick={() => openModal(card)}
              >
                {/* Left accent bar */}
                <div className="oc-accent-bar" />

                {/* Checkbox */}
                <label className="order-card-check" onClick={e => e.stopPropagation()}>
                  <input type="checkbox"
                    checked={selected.has(card.doc_id)}
                    onChange={e => toggleSelect(e, card.doc_id)} />
                </label>

                {/* Content */}
                <div className="order-card-body">
                  <div className="order-card-top">
                    <div className="order-card-entity">
                      {card.entity.replace(/^M\/s\.?\s*/i, '')}
                    </div>
                    <div className="order-card-badges">
                      {(card.action_types || []).map(a => <ActionBadge key={a} type={a} />)}
                      {fmtPenalty(card.penalty_pkr) && (
                        <span className="order-card-penalty">{fmtPenalty(card.penalty_pkr)}</span>
                      )}
                    </div>
                  </div>
                  <div className="order-card-meta">
                    {card.order_reference && (
                      <span className="ocm-ref" title={card.order_reference}>
                        {card.order_reference.length > 50
                          ? card.order_reference.slice(0, 50) + '…'
                          : card.order_reference}
                      </span>
                    )}
                    {card.order_reference && (card.order_date || card.acts?.length) && (
                      <span className="ocm-sep">·</span>
                    )}
                    {card.order_date && (
                      <span className="ocm-date">{fmtDate(card.order_date)}</span>
                    )}
                    {(card.acts || []).slice(0, 1).map(a => (
                      <span key={a} className="ocm-sep">·</span>
                    ))}
                    {(card.acts || []).slice(0, 1).map(a => (
                      <span key={a} className="ocm-act">{a}</span>
                    ))}
                    {card.issuing_officer && (
                      <>
                        <span className="ocm-sep">·</span>
                        <span className="ocm-officer">{card.issuing_officer.split(',')[0]}</span>
                      </>
                    )}
                  </div>
                  {(card.violations || []).length > 0 && (
                    <div className="order-card-violation">
                      {card.violations[0]}
                      {card.violations.length > 1 && (
                        <span className="ocv-more"> +{card.violations.length - 1} more</span>
                      )}
                    </div>
                  )}

                  {/* AI Summary CTA */}
                  <button
                    className={`oc-summary-btn ${generatingId === card.doc_id ? 'oc-summary-btn--loading' : ''}`}
                    onClick={e => { e.stopPropagation(); openModal(card) }}
                    disabled={!!generatingId}
                  >
                    {generatingId === card.doc_id ? (
                      <><span className="oc-spinner" /> Generating summary…</>
                    ) : (
                      <><BookOpen size={12} /> View AI Summary</>
                    )}
                  </button>
                </div>

                {/* Doc icon */}
                <div className="order-card-icon"><FileText size={18} /></div>
              </div>
            ))
          )}
        </div>

        {/* Filter panel */}
        <FilterPanel
          cards={cards}
          filters={filters}
          onChange={setFilters}
          onClear={() => setFilters(EMPTY_FILTERS)}
          count={filtered.length}
        />
      </div>

      {/* ── Summary modal ── */}
      {modalId && (
        <SummaryModal
          docId={modalId}
          filename={modalFile}
          onClose={() => { setModalId(null); setModalFile('') }}
          token={token}
        />
      )}

      {/* ── Consolidated report panel (modal) ── */}
      {showConPanel && (
        <div className="modal-backdrop" onClick={e => { if (e.target===e.currentTarget) setShowConPanel(false) }}>
          <div className="con-modal">
            <div className="con-modal-header">
              <h3>Generate Consolidated Report</h3>
              <button className="modal-close-btn" onClick={() => setShowConPanel(false)}><X size={15} /></button>
            </div>
            <div className="con-modal-body">
              <p className="con-modal-desc">
                <strong>{selected.size} orders</strong> selected.
                An AI-generated factual pattern analysis will be produced across all selected cases.
              </p>
              <div className="con-scope-row">
                <label>Report Scope / Title <span className="con-scope-hint">(optional)</span></label>
                <input className="con-scope-input" value={scope} onChange={e => setScope(e.target.value)}
                  placeholder="e.g. Section 510 enforcement, 2025" />
              </div>
              <div className="con-selected-list">
                {cards.filter(c => selected.has(c.doc_id)).map(c => (
                  <div key={c.doc_id} className="con-selected-item">
                    <span className="con-sel-entity">{c.entity.replace(/^M\/s\.?\s*/i,'')}</span>
                    <span className="con-sel-date">{fmtDate(c.order_date)}</span>
                  </div>
                ))}
              </div>
              {conError && <p className="con-error">{conError}</p>}
              <button className="action-btn primary con-generate-btn"
                onClick={generateConsolidated} disabled={consolidating}>
                {consolidating ? 'Generating…' : 'Generate Report'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Consolidated result (full modal) ── */}
      {consolidated && (
        <ConsolidatedDisplay data={consolidated} onClose={() => setConsolidated(null)} />
      )}
    </div>
  )
}

// ─── Consolidated Display ─────────────────────────────────────────────────────

function ConsolidatedDisplay({ data, onClose }) {
  const con   = data.consolidated || {}
  const stats = con.aggregate_stats || {}

  function downloadText() {
    const lines = [
      'SECP ADJUDICATION ORDERS — CONSOLIDATED SUMMARY',
      `Orders: ${data.count}  Scope: ${data.scope || 'Selected orders'}`,
      '',
      con.scope_statement || '',
      '', con.introduction || '',
      '',
      'AGGREGATE STATISTICS',
      stats.total_monetary_penalties_pkr ? `Total penalties: PKR ${Number(stats.total_monetary_penalties_pkr).toLocaleString()}` : '',
      `Most cited sections: ${(stats.most_cited_sections||[]).join(', ')}`,
      `Most common action: ${stats.most_common_action || ''}`,
      '',
      'PATTERNS',
      ...(con.patterns||[]).map(p => `• ${p.pattern}`),
      '',
      'INDIVIDUAL CASES',
      ...(con.individual_cases||[]).map(c => `[${c.reference||'N/A'}]  ${c.respondent}  ${c.date||''}  ${c.outcome||''}`),
      '',
      'CONCLUSION', con.conclusion || '',
    ]
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob)
    a.download = `SECP_Consolidated_${data.count}_orders.txt`; a.click()
  }

  return (
    <div className="modal-backdrop" onClick={e => { if (e.target===e.currentTarget) onClose() }}>
      <div className="modal-box">
        <div className="modal-header">
          <div className="modal-header-left">
            <h2>Consolidated Summary — {data.count} Orders</h2>
            {data.scope && <div className="modal-meta-row"><span>{data.scope}</span></div>}
          </div>
          <div className="modal-header-right">
            <button className="action-btn" onClick={downloadText}><Download size={13} /> Download .txt</button>
            <button className="modal-close-btn" onClick={onClose}><X size={15} /></button>
          </div>
        </div>
        <div className="modal-body">
          <div className="modal-sections">
            <div className="modal-section">
              <div className="modal-section-title">Scope Statement</div>
              <div className="modal-section-body"><p>{con.scope_statement||'—'}</p></div>
            </div>
            <div className="modal-section">
              <div className="modal-section-title">Introduction</div>
              <div className="modal-section-body"><p>{con.introduction||'—'}</p></div>
            </div>
            <div className="modal-section">
              <div className="modal-section-title">Aggregate Statistics</div>
              <div className="modal-section-body">
                <div className="con-stats">
                  <div className="con-stat">
                    <span className="con-stat-label">Total Penalties</span>
                    <span className="con-stat-val">
                      {stats.total_monetary_penalties_pkr
                        ? `PKR ${Number(stats.total_monetary_penalties_pkr).toLocaleString()}`
                        : 'Non-monetary / Mixed'}
                    </span>
                  </div>
                  <div className="con-stat">
                    <span className="con-stat-label">Most Cited Sections</span>
                    <span className="con-stat-val">{(stats.most_cited_sections||[]).join(', ')||'—'}</span>
                  </div>
                  <div className="con-stat">
                    <span className="con-stat-label">Most Common Action</span>
                    <span className="con-stat-val">{stats.most_common_action||'—'}</span>
                  </div>
                </div>
              </div>
            </div>
            {con.patterns?.length > 0 && (
              <div className="modal-section">
                <div className="modal-section-title">Patterns Identified</div>
                <div className="modal-section-body">
                  <ul className="con-patterns">
                    {con.patterns.map((p,i) => (
                      <li key={i}>{p.pattern}
                        {p.supporting_cases?.length > 0 && (
                          <span className="con-pattern-cases"> [{p.supporting_cases.join(', ')}]</span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            )}
            {con.individual_cases?.length > 0 && (
              <div className="modal-section">
                <div className="modal-section-title">Individual Cases ({con.individual_cases.length})</div>
                <div className="modal-section-body">
                  <div className="con-cases">
                    {con.individual_cases.map((c,i) => (
                      <div key={i} className="con-case-row">
                        <div className="con-case-ref">{c.reference||'N/A'}</div>
                        <div className="con-case-body">
                          <strong>{c.respondent}</strong>
                          {c.date && <span className="con-case-date"> — {c.date}</span>}
                          {c.outcome && <p className="con-case-outcome">{c.outcome}</p>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
            <div className="modal-section">
              <div className="modal-section-title">Conclusion</div>
              <div className="modal-section-body"><p>{con.conclusion||'—'}</p></div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ─── Upload Tab ───────────────────────────────────────────────────────────────

const UPLOAD_STEPS = [
  { key: 'upload',  label: 'Uploading PDF…'               },
  { key: 'extract', label: 'Extracting text (OCR)…'       },
  { key: 'analyze', label: 'Analyzing with GPT-4o-mini…'  },
  { key: 'build',   label: 'Building structured summary…' },
]

function UploadTab({ token }) {
  const authH = token ? { Authorization: `Bearer ${token}` } : {}
  const [step,     setStep]    = useState(null)
  const [summary,  setSummary] = useState(null)
  const [filename, setFilename] = useState('')
  const [error,    setError]   = useState(null)
  const [dragging, setDragging] = useState(false)
  const fileRef = useRef(null)

  function reset() { setStep(null); setSummary(null); setError(null); setFilename('') }

  async function handleFile(file) {
    if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
      setError('Please upload a PDF file.'); return
    }
    setError(null); setFilename(file.name); setSummary(null)
    setStep('upload')
    const form = new FormData(); form.append('file', file)
    try {
      setStep('extract'); await new Promise(r => setTimeout(r, 400))
      setStep('analyze')
      const res = await fetch('/api/summarize', { method: 'POST', body: form, headers: authH })
      setStep('build'); await new Promise(r => setTimeout(r, 300))
      if (!res.ok) throw new Error((await res.json().catch(()=>({}))).detail || `HTTP ${res.status}`)
      setSummary((await res.json()).summary); setStep(null)
    } catch (err) { setError(err.message); setStep(null) }
  }

  // Display uploaded summary inline (not in modal)
  if (summary) {
    const hdr = summary.case_header || {}
    const sections = [
      { title: 'Case Background',          key: 'case_background'          },
      { title: 'Key Facts',                key: 'key_facts',   isList: true },
      { title: 'Violation Identified',     key: 'violation_identified'     },
      { title: 'Legal Provisions Applied', key: 'legal_provisions_applied' },
      { title: "SECP's Determination",     key: 'secp_determination'       },
      { title: 'Penalty / Sanction',       key: 'penalty_or_sanction'      },
      { title: 'Source Citation',          key: 'source_citation', isPre: true },
      { title: 'Current Status',           key: 'current_status'           },
    ]
    return (
      <div className="upload-result-wrap">
        <div className="upload-result-header">
          <div>
            <h2 className="upload-result-entity">{hdr.respondent || filename}</h2>
            <p className="upload-result-sub">{filename}</p>
          </div>
          <div style={{display:'flex',gap:8}}>
            <button className="action-btn" onClick={() => {
              const lines = ['SECP SUMMARY', `Source: ${filename}`, '']
              const blob = new Blob([lines.join('\n')], {type:'text/plain'})
              const a = document.createElement('a'); a.href=URL.createObjectURL(blob)
              a.download=filename.replace('.pdf','_summary.txt'); a.click()
            }}>Download .txt</button>
            <button className="action-btn" onClick={reset}>New Upload</button>
          </div>
        </div>
        <div className="upload-result-meta">
          {hdr.order_reference && <span>{hdr.order_reference}</span>}
          {hdr.order_date      && <span>·</span>}
          {hdr.order_date      && <span>{hdr.order_date}</span>}
          {hdr.laws_applied    && <span>·</span>}
          {hdr.laws_applied    && <span>{hdr.laws_applied}</span>}
        </div>
        <div className="modal-sections upload-sections">
          {sections.map(sec => {
            const val = summary[sec.key]
            const empty = !val || val === 'Not available in this order.'
            return (
              <div key={sec.key} className="modal-section">
                <div className="modal-section-title">{sec.title}</div>
                <div className="modal-section-body">
                  {empty ? <p className="sum-empty">Not recorded.</p>
                    : sec.isList
                    ? <ol className="sum-list">{(Array.isArray(val)?val:[val]).map((it,i)=><li key={i}>{it}</li>)}</ol>
                    : sec.isPre ? <pre className="sum-pre">{val}</pre>
                    : <p>{val}</p>}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    )
  }

  return (
    <div className="upload-tab-content">
      {!step && (
        <>
          <div
            className={`drop-zone ${dragging ? 'drag-over' : ''}`}
            onDragOver={e => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={e => { e.preventDefault(); setDragging(false); handleFile(e.dataTransfer.files[0]) }}
            onClick={() => fileRef.current?.click()}
          >
            <div className="dz-icon"><Upload size={36} strokeWidth={1.5} /></div>
            <h3>Upload an Adjudication Order</h3>
            <p>Drag &amp; drop a PDF, or click to browse</p>
            <p className="hint">For orders not yet in the database — generates a 9-component summary on-demand.</p>
            <button className="browse-btn" onClick={e => { e.stopPropagation(); fileRef.current?.click() }}>
              Browse Files
            </button>
          </div>
          <input ref={fileRef} type="file" accept=".pdf" style={{display:'none'}}
            onChange={e => handleFile(e.target.files[0])} />
          {error && <p className="upload-error">{error}</p>}
        </>
      )}
      {step && (
        <div className="upload-progress">
          <p className="upload-filename">Processing: {filename}</p>
          <div className="progress-bar-wrap">
            {UPLOAD_STEPS.map((s, i) => {
              const idx = UPLOAD_STEPS.findIndex(x => x.key === step)
              return (
                <div key={s.key} className={`progress-step ${i<idx?'done':i===idx?'active':''}`}>
                  <div className="step-dot">{i < idx ? <CheckCircle size={12} /> : null}</div>
                  {s.label}
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

// ─── Main ─────────────────────────────────────────────────────────────────────

export default function PDFSummarizer({ token }) {
  const [subTab, setSubTab] = useState('library')

  return (
    <div className="summarize-page">
      <div className="sum-subtabs">
        <button className={`sum-subtab ${subTab==='library'?'active':''}`}
          onClick={() => setSubTab('library')}>Case Summary Library</button>
        <button className={`sum-subtab ${subTab==='upload'?'active':''}`}
          onClick={() => setSubTab('upload')}>Upload New PDF</button>
      </div>
      {subTab === 'library' ? <SummaryLibrary token={token} /> : <UploadTab token={token} />}
    </div>
  )
}
