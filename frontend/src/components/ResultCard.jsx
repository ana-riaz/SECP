import { useState } from 'react'

function relevanceClass(pct) {
  if (pct >= 60) return 'high'
  if (pct >= 35) return 'medium'
  return 'low'
}

function fmtDate(iso) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('en-PK', {
      day: 'numeric', month: 'long', year: 'numeric',
    })
  } catch { return iso }
}

const EXCHANGE_LABEL = {
  'Listed Company':           'PSX-listed',
  'Broker':                   'Broker',
  'Asset Management Company': 'AMC',
  'NBFC':                     'NBFC',
  'Insurance Company':        'Insurance',
}

export default function ResultCard({ result, index }) {
  const [open, setOpen] = useState(index === 0)

  const entity    = result.entity_names?.join(', ') || result.filename
  const pct       = result.relevance_pct ?? 0
  const exchLabel = EXCHANGE_LABEL[result.entity_category]

  return (
    <div className="result-card">
      {/* Header — always visible */}
      <div className="result-card-header" onClick={() => setOpen(o => !o)}>
        <div className="result-rank">{result.rank}</div>
        <div className="result-entity">
          {entity}
          {exchLabel && <span className="exchange-badge">{exchLabel}</span>}
        </div>
        {pct > 0 && <span className={`relevance-badge ${relevanceClass(pct)}`}>{pct}% match</span>}
        <span className="expand-icon">{open ? '▲' : '▼'}</span>
      </div>

      {/* Body — collapsible */}
      {open && (
        <div className="result-card-body">
          {/* Meta row */}
          <div className="meta-row">
            <div className="meta-item">
              <span className="lbl">Reference</span>
              <span className="val">{result.order_reference || '—'}</span>
            </div>
            <div className="meta-item">
              <span className="lbl">Order Date</span>
              <span className="val">{fmtDate(result.order_date)}</span>
            </div>
            <div className="meta-item">
              <span className="lbl">Penalty</span>
              <span className={`val ${result.penalty_display?.startsWith('PKR') ? 'penalty' : ''}`}>
                {result.penalty_display || 'Not specified'}
              </span>
            </div>
            <div className="meta-item">
              <span className="lbl">Issuing Officer</span>
              <span className="val">{result.issuing_officer || '—'}</span>
            </div>
          </div>

          {/* Action types */}
          {result.action_types?.length > 0 && (
            <>
              <div className="section-label">Action Type</div>
              <div className="pills">
                {result.action_types.map((a, i) => (
                  <span key={i} className="pill action">{a}</span>
                ))}
              </div>
            </>
          )}

          {/* Legal provisions */}
          {result.legal_provisions?.length > 0 && (
            <>
              <div className="section-label">Legal Provisions</div>
              <div className="pills">
                {result.legal_provisions.map((p, i) => (
                  <span key={i} className="pill">
                    Sec {p.section} — {p.act}
                  </span>
                ))}
              </div>
            </>
          )}

          {/* Violations */}
          {result.violations?.length > 0 && (
            <>
              <div className="section-label">Violations</div>
              <div className="pills">
                {result.violations.map((v, i) => (
                  <span key={i} className="pill violation">{v}</span>
                ))}
              </div>
            </>
          )}

          {/* Summary */}
          {result.case_summary && (
            <>
              <div className="section-label">Summary</div>
              <p className="summary-text">{result.case_summary}</p>
            </>
          )}

          {/* Key facts */}
          {result.key_facts?.length > 0 && (
            <>
              <div className="section-label">Key Facts</div>
              <ul className="facts-list">
                {result.key_facts.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            </>
          )}

          {/* Respondents */}
          {result.individual_respondents?.length > 0 && (
            <>
              <div className="section-label">Respondents</div>
              <div className="pills">
                {result.individual_respondents.map((r, i) => (
                  <span key={i} className="pill">{r}</span>
                ))}
              </div>
            </>
          )}

          {/* Source */}
          <div className="source-link">
            Source: secp.gov.pk &rsaquo; Document Center &rsaquo; Adjudication Orders
            &nbsp;&nbsp;|&nbsp;&nbsp; {result.filename}
          </div>
        </div>
      )}
    </div>
  )
}
