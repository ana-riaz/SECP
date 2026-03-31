import { useEffect, useState } from 'react'

export default function Sidebar({ onQueryClick }) {
  const [stats, setStats]   = useState(null)
  const [docs,  setDocs]    = useState([])

  useEffect(() => {
    fetch('/api/stats').then(r => r.json()).then(setStats).catch(() => {})
    fetch('/api/documents').then(r => r.json()).then(setDocs).catch(() => {})
  }, [])

  const fmtDate = iso => iso ? iso.slice(0, 7) : ''

  return (
    <aside className="sidebar">
      <div className="sidebar-logo">
        <h2>SECP RAG</h2>
        <p>Adjudication Research Assistant</p>
      </div>

      {stats && (
        <div className="sidebar-stats">
          <div className="stat-box">
            <div className="val">{stats.doc_count}</div>
            <div className="lbl">Documents</div>
          </div>
          <div className="stat-box">
            <div className="val">{stats.chunk_count}</div>
            <div className="lbl">Chunks</div>
          </div>
          <div className="stat-box">
            <div className="val">{(stats.avg_confidence * 100).toFixed(0)}%</div>
            <div className="lbl">Avg Conf</div>
          </div>
          <div className="stat-box">
            <div className="val">{stats.pending_review}</div>
            <div className="lbl">For Review</div>
          </div>
        </div>
      )}

      <div className="sidebar-section-title">Ingested Orders</div>

      <div className="doc-list">
        {Object.entries(
          docs.reduce((acc, doc) => {
            const cat = doc.category || 'Uncategorized'
            if (!acc[cat]) acc[cat] = []
            acc[cat].push(doc)
            return acc
          }, {})
        ).map(([category, catDocs]) => (
          <div key={category}>
            <div style={{
              fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
              letterSpacing: '.5px', color: 'rgba(255,255,255,.35)',
              padding: '10px 10px 4px', marginTop: 4,
            }}>
              {category}
            </div>
            {catDocs.map((doc, i) => (
              <div
                key={i}
                className="doc-item"
                title={doc.filename}
                onClick={() => onQueryClick(`Find order for ${doc.entity}`)}
              >
                <div className="doc-entity">
                  <span className={`status-dot ${doc.status}`} />
                  {doc.entity.replace('M/s. ', '').replace('Ms. ', '')}
                </div>
                <div className="doc-meta">
                  {fmtDate(doc.date)}
                  {doc.penalty ? ` · PKR ${Number(doc.penalty).toLocaleString()}` : ''}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>
    </aside>
  )
}
