"""
Phase 3 - Knowledge Database Layer
====================================
SQLite-backed structured store for adjudication order extractions.

Tables
------
  orders          - current canonical extraction record (one row per doc)
  field_confidence- per-field confidence scores and source text locations
  field_history   - append-only audit trail of every value change
  review_queue    - materialised list of fields awaiting human review

Design principles
-----------------
  - orders holds the mutable current state; field_history is immutable
  - JSON arrays/objects serialised as TEXT (SQLite json_extract() works on them)
  - WAL journal mode for safe concurrent access (review_tool + extractor)
  - upsert_order() diffs against previous values and writes history entries
    atomically inside a transaction
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import config

# ---------------------------------------------------------------------------
# All 19 tracked fields (Phase 2 + Phase 3 additions)
# ---------------------------------------------------------------------------
ALL_FIELDS = [
    "entity_names", "individual_respondents", "entity_category", "sector",
    "order_reference", "order_date", "date_of_notice", "date_of_hearing",
    "issuing_officer", "penalty_pkr", "penalty_note",
    "violations", "legal_provisions", "action_types",
    "key_facts", "case_summary", "secp_findings", "final_outcome",
]

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                  TEXT NOT NULL UNIQUE,
    filename                TEXT NOT NULL,
    extraction_version      INTEGER NOT NULL DEFAULT 1,
    extracted_at            TEXT NOT NULL,
    extracted_by            TEXT NOT NULL DEFAULT 'llm',
    extraction_model        TEXT,
    review_status           TEXT NOT NULL DEFAULT 'pending',
    reviewed_by             TEXT,
    reviewed_at             TEXT,

    -- 18 extracted fields (lists/objects stored as JSON text)
    entity_names            TEXT,
    individual_respondents  TEXT,
    entity_category         TEXT,
    sector                  TEXT,
    order_reference         TEXT,
    order_date              TEXT,
    date_of_notice          TEXT,
    date_of_hearing         TEXT,
    issuing_officer         TEXT,
    penalty_pkr             REAL,
    penalty_note            TEXT,
    violations              TEXT,
    legal_provisions        TEXT,
    action_types            TEXT,
    key_facts               TEXT,
    case_summary            TEXT,
    secp_findings           TEXT,
    final_outcome           TEXT
);

CREATE TABLE IF NOT EXISTS field_confidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES orders(id),
    field_name          TEXT NOT NULL,
    confidence          REAL NOT NULL,
    needs_review        INTEGER NOT NULL DEFAULT 0,
    source_char_start   INTEGER,
    source_char_end     INTEGER,
    source_excerpt      TEXT,
    UNIQUE(order_id, field_name)
);

CREATE TABLE IF NOT EXISTS field_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    doc_id          TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    change_reason   TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    doc_id          TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    confidence      REAL NOT NULL,
    queue_status    TEXT NOT NULL DEFAULT 'open',
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     TEXT,
    UNIQUE(order_id, field_name)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialise(val: Any) -> Any:
    """Store lists/dicts as JSON strings; pass scalars through."""
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False)
    return val


def _deserialise(field: str, val: Any) -> Any:
    """Restore lists/dicts from JSON strings."""
    if val is None:
        return val
    LIST_FIELDS = {
        "entity_names", "individual_respondents", "date_of_hearing",
        "violations", "legal_provisions", "action_types", "key_facts",
    }
    if field in LIST_FIELDS and isinstance(val, str):
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            return val
    return val


# ---------------------------------------------------------------------------
class KnowledgeDB:

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or config.KNOWLEDGE_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Connection ──────────────────────────────────────────────────────────
    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ── Upsert ──────────────────────────────────────────────────────────────
    def upsert_order(
        self,
        doc_id:           str,
        filename:         str,
        fields:           dict[str, Any],
        confidence_scores: dict[str, float],
        text_locations:   dict[str, dict],
        extraction_model: str = config.STRUCTURED_EXTRACTION_MODEL,
        changed_by:       str = "llm",
    ) -> int:
        """
        Insert or update an order record.

        - On first insert: creates the row + populates field_confidence + review_queue
        - On update: diffs against previous values, logs changes to field_history,
          increments extraction_version, refreshes field_confidence + review_queue
        All operations execute inside a single transaction.
        """
        now = _now()

        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, extraction_version FROM orders WHERE doc_id = ?",
                (doc_id,)
            ).fetchone()

            if existing:
                order_id = existing["id"]
                new_version = existing["extraction_version"] + 1

                # Diff and log changes to field_history
                old_row = conn.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()

                for field in ALL_FIELDS:
                    new_val = _serialise(fields.get(field))
                    old_val = old_row[field] if field in old_row.keys() else None
                    if str(old_val) != str(new_val):
                        conn.execute(
                            """INSERT INTO field_history
                               (order_id, doc_id, field_name, old_value, new_value,
                                changed_by, changed_at)
                               VALUES (?,?,?,?,?,?,?)""",
                            (order_id, doc_id, field,
                             old_val, new_val, changed_by, now)
                        )

                # Update orders row
                set_parts = ", ".join(f"{f} = ?" for f in ALL_FIELDS)
                vals = [_serialise(fields.get(f)) for f in ALL_FIELDS]
                conn.execute(
                    f"""UPDATE orders
                        SET {set_parts},
                            extraction_version = ?,
                            extracted_at = ?,
                            extraction_model = ?
                        WHERE id = ?""",
                    vals + [new_version, now, extraction_model, order_id]
                )

            else:
                # Fresh insert
                field_placeholders = ", ".join("?" for _ in ALL_FIELDS)
                field_names = ", ".join(ALL_FIELDS)
                vals = [_serialise(fields.get(f)) for f in ALL_FIELDS]
                cur = conn.execute(
                    f"""INSERT INTO orders
                        (doc_id, filename, extracted_at, extraction_model,
                         {field_names})
                        VALUES (?,?,?,?,{field_placeholders})""",
                    [doc_id, filename, now, extraction_model] + vals
                )
                order_id = cur.lastrowid

                # Log initial extraction as field_history entries
                for field in ALL_FIELDS:
                    new_val = _serialise(fields.get(field))
                    if new_val is not None:
                        conn.execute(
                            """INSERT INTO field_history
                               (order_id, doc_id, field_name, old_value, new_value,
                                changed_by, changed_at)
                               VALUES (?,?,?,NULL,?,?,?)""",
                            (order_id, doc_id, field, new_val, changed_by, now)
                        )

            # Refresh field_confidence (delete + re-insert for idempotency)
            conn.execute("DELETE FROM field_confidence WHERE order_id = ?", (order_id,))
            conn.execute(
                "DELETE FROM review_queue WHERE order_id = ?", (order_id,)
            )

            threshold = config.CONFIDENCE_REVIEW_THRESHOLD
            for field, score in confidence_scores.items():
                loc = text_locations.get(field, {})
                needs_review = 1 if score < threshold else 0
                conn.execute(
                    """INSERT OR REPLACE INTO field_confidence
                       (order_id, field_name, confidence, needs_review,
                        source_char_start, source_char_end, source_excerpt)
                       VALUES (?,?,?,?,?,?,?)""",
                    (order_id, field, score, needs_review,
                     loc.get("char_start"), loc.get("char_end"),
                     loc.get("excerpt"))
                )
                if needs_review:
                    conn.execute(
                        """INSERT OR REPLACE INTO review_queue
                           (order_id, doc_id, field_name, confidence, created_at)
                           VALUES (?,?,?,?,?)""",
                        (order_id, doc_id, field, score, now)
                    )

        return order_id

    # ── Read ────────────────────────────────────────────────────────────────
    def get_order(self, doc_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            for field in ALL_FIELDS:
                result[field] = _deserialise(field, result.get(field))
            return result

    def get_all_orders(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY extracted_at DESC"
            ).fetchall()
            out = []
            for row in rows:
                r = dict(row)
                for f in ALL_FIELDS:
                    r[f] = _deserialise(f, r.get(f))
                out.append(r)
            return out

    def get_field_confidence(self, doc_id: str) -> dict[str, dict]:
        with self._conn() as conn:
            order = conn.execute(
                "SELECT id FROM orders WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if not order:
                return {}
            rows = conn.execute(
                "SELECT * FROM field_confidence WHERE order_id = ?",
                (order["id"],)
            ).fetchall()
            return {
                r["field_name"]: {
                    "confidence":   r["confidence"],
                    "needs_review": bool(r["needs_review"]),
                    "char_start":   r["source_char_start"],
                    "char_end":     r["source_char_end"],
                    "excerpt":      r["source_excerpt"],
                }
                for r in rows
            }

    def get_flagged_docs(self) -> list[dict]:
        """Return all docs that have at least one open review_queue entry."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT o.doc_id, o.filename, o.review_status,
                          COUNT(rq.id) as flagged_count,
                          MIN(rq.confidence) as min_confidence
                   FROM orders o
                   JOIN review_queue rq ON rq.order_id = o.id
                   WHERE rq.queue_status = 'open'
                   GROUP BY o.id
                   ORDER BY flagged_count DESC""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_review_queue(self, doc_id: Optional[str] = None) -> list[dict]:
        with self._conn() as conn:
            if doc_id:
                rows = conn.execute(
                    """SELECT rq.*, o.filename
                       FROM review_queue rq
                       JOIN orders o ON o.id = rq.order_id
                       WHERE rq.doc_id = ? AND rq.queue_status = 'open'
                       ORDER BY rq.confidence ASC""",
                    (doc_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT rq.*, o.filename
                       FROM review_queue rq
                       JOIN orders o ON o.id = rq.order_id
                       WHERE rq.queue_status = 'open'
                       ORDER BY rq.confidence ASC""",
                ).fetchall()
            return [dict(r) for r in rows]

    def get_field_history(self, doc_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM field_history
                   WHERE doc_id = ?
                   ORDER BY changed_at DESC""",
                (doc_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Edit (human review) ──────────────────────────────────────────────────
    def update_field(
        self,
        doc_id:        str,
        field_name:    str,
        new_value:     Any,
        changed_by:    str,
        change_reason: str = "",
    ) -> bool:
        """
        Update a single field value. Logs to field_history. Marks record as
        having had human edits (review_status -> 'reviewed').
        """
        now = _now()
        with self._conn() as conn:
            order = conn.execute(
                "SELECT id FROM orders WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if not order:
                return False
            order_id = order["id"]

            # Get old value
            row = conn.execute(
                f"SELECT {field_name} FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            old_val = row[field_name] if row else None
            new_ser = _serialise(new_value)

            # Update field
            conn.execute(
                f"UPDATE orders SET {field_name} = ?, review_status = 'reviewed', "
                f"reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (new_ser, changed_by, now, order_id)
            )

            # Audit trail
            conn.execute(
                """INSERT INTO field_history
                   (order_id, doc_id, field_name, old_value, new_value,
                    changed_by, changed_at, change_reason)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (order_id, doc_id, field_name, old_val, new_ser,
                 changed_by, now, change_reason)
            )

            # Resolve the review_queue entry for this field
            conn.execute(
                """UPDATE review_queue
                   SET queue_status = 'resolved', resolved_at = ?, resolved_by = ?
                   WHERE order_id = ? AND field_name = ?""",
                (now, changed_by, order_id, field_name)
            )
        return True

    def approve_order(self, doc_id: str, reviewer: str) -> bool:
        """Mark a full order as approved (all fields accepted)."""
        now = _now()
        with self._conn() as conn:
            order = conn.execute(
                "SELECT id FROM orders WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if not order:
                return False
            conn.execute(
                """UPDATE orders
                   SET review_status = 'approved', reviewed_by = ?, reviewed_at = ?
                   WHERE id = ?""",
                (reviewer, now, order["id"])
            )
            conn.execute(
                """UPDATE review_queue
                   SET queue_status = 'resolved', resolved_at = ?, resolved_by = ?
                   WHERE order_id = ?""",
                (now, reviewer, order["id"])
            )
        return True

    # ── Stats ────────────────────────────────────────────────────────────────
    def stats(self) -> dict:
        with self._conn() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE review_status = 'pending'"
            ).fetchone()[0]
            reviewed = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE review_status = 'reviewed'"
            ).fetchone()[0]
            approved = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE review_status = 'approved'"
            ).fetchone()[0]
            open_q = conn.execute(
                "SELECT COUNT(*) FROM review_queue WHERE queue_status = 'open'"
            ).fetchone()[0]
            avg_conf = conn.execute(
                "SELECT AVG(confidence) FROM field_confidence"
            ).fetchone()[0]
        return {
            "total_records":       total,
            "pending_review":      pending,
            "human_reviewed":      reviewed,
            "approved":            approved,
            "open_queue_items":    open_q,
            "avg_confidence":      round(avg_conf, 3) if avg_conf else 0.0,
        }

    # ── Export ───────────────────────────────────────────────────────────────
    def export_json(self, path: Optional[Path] = None) -> Path:
        records = self.get_all_orders()
        out = path or config.DATA_DIR / "knowledge_export.json"
        out.write_text(
            json.dumps(records, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8"
        )
        return out

    def export_csv(self, path: Optional[Path] = None) -> Path:
        import csv
        records = self.get_all_orders()
        out = path or config.DATA_DIR / "knowledge_export.csv"
        scalar_fields = [
            "doc_id", "filename", "extraction_version", "extracted_at",
            "review_status", "order_reference", "order_date", "date_of_notice",
            "entity_category", "sector", "issuing_officer", "penalty_pkr",
            "penalty_note", "final_outcome",
        ]
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=scalar_fields + ["entity_names_str",
                                                               "action_types_str",
                                                               "violations_str"])
            w.writeheader()
            for r in records:
                row = {k: r.get(k) for k in scalar_fields}
                row["entity_names_str"]  = "; ".join(r.get("entity_names") or [])
                row["action_types_str"]  = "; ".join(r.get("action_types") or [])
                row["violations_str"]    = "; ".join((r.get("violations") or [])[:3])
                w.writerow(row)
        return out
