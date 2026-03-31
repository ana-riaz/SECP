"""
Phase 3 - Structured Knowledge Extraction
==========================================
Reads every processed_docs JSON, runs GPT-4o-mini to extract all 18 fields
with per-field confidence scores, locates each value in the source text,
and stores everything in the SQLite knowledge database (data/knowledge.db).

New fields over Phase 2 (extract_metadata.py)
----------------------------------------------
  date_of_notice  : Show Cause Notice issue date
  date_of_hearing : list of hearing dates
  secp_findings   : Commission's legal reasoning summary
  final_outcome   : operative result of proceedings

All 18 fields have confidence scores (0.0-1.0).
Fields with confidence < CONFIDENCE_REVIEW_THRESHOLD are queued for review.

Usage
-----
  python extract_structured.py              # process docs not yet in DB
  python extract_structured.py --force      # re-extract everything
  python extract_structured.py --doc <filename>
  python extract_structured.py --dry-run    # print extraction, no DB write
  python extract_structured.py --stats      # show DB statistics
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

import config
from knowledge_db import KnowledgeDB, ALL_FIELDS

MAX_TEXT_CHARS = 28_000   # GPT-4o-mini context limit guard

# ==============================================================================
# LLM Prompt
# ==============================================================================

_SYSTEM = """\
You are a legal analyst specialising in SECP (Securities and Exchange Commission
of Pakistan) adjudication orders.

Extract structured information from the adjudication order text provided.
For EACH field return a JSON object with exactly two keys:
  "value"     : the extracted value (type specified below)
  "confidence": float 0.0-1.0, where:
                  1.0 = verbatim in text
                  0.8 = clearly implied
                  0.6 = reasonable inference, some ambiguity
                  0.4 = uncertain, multiple interpretations
                  0.0 = not found / genuinely absent

Return ONLY valid JSON. No markdown. No explanation. Never invent information.\
"""

_SCHEMA = """\
Extract the following fields from the adjudication order text:

{
  "entity_names": {
    "value": ["list of company/entity names that are respondents or noticees"],
    "confidence": 0.0
  },
  "individual_respondents": {
    "value": ["list of named individuals with designation, e.g. 'Mr. Ali Khan, CEO'"],
    "confidence": 0.0
  },
  "entity_category": {
    "value": "one of: Listed Company | Unlisted Company | Auditor | Individual | Broker | NBFC | Insurance Company | Other",
    "confidence": 0.0
  },
  "sector": {
    "value": "one of: Corporate | Securities & Capital Markets | Insurance | NBFC | Other",
    "confidence": 0.0
  },
  "order_reference": {
    "value": "official order/case/SCN reference number as written in the document",
    "confidence": 0.0
  },
  "order_date": {
    "value": "YYYY-MM-DD — official date of the adjudication order",
    "confidence": 0.0
  },
  "date_of_notice": {
    "value": "YYYY-MM-DD — date the Show Cause Notice (SCN) was issued, or null",
    "confidence": 0.0
  },
  "date_of_hearing": {
    "value": ["YYYY-MM-DD list — all hearing dates mentioned in the order, or []"],
    "confidence": 0.0
  },
  "issuing_officer": {
    "value": "Full Name and Designation of the officer who signed/issued the order",
    "confidence": 0.0
  },
  "penalty_pkr": {
    "value": "numeric total penalty in PKR, or null if no monetary penalty",
    "confidence": 0.0
  },
  "penalty_note": {
    "value": "description if action is non-monetary (e.g. 'stern warning'), or null",
    "confidence": 0.0
  },
  "violations": {
    "value": ["list of specific regulatory violations cited in the order"],
    "confidence": 0.0
  },
  "legal_provisions": {
    "value": [{"section": "section number", "act": "full Act name", "clause": "sub-clause or null"}],
    "confidence": 0.0
  },
  "action_types": {
    "value": ["subset of: Penalty | Warning | Show Cause Notice | Settlement | Licensing Action | Compliance Direction"],
    "confidence": 0.0
  },
  "key_facts": {
    "value": ["list of 3-6 key factual background points from the case"],
    "confidence": 0.0
  },
  "case_summary": {
    "value": "2-3 sentence plain-language summary of what happened and the outcome",
    "confidence": 0.0
  },
  "secp_findings": {
    "value": "The Commission's legal reasoning — the full paragraph(s) starting with phrases like 'I have gone through', 'I have taken into consideration', 'In view of the foregoing', or 'I am of the considered view'. Include all reasoning text up to but not including the operative penalty clause.",
    "confidence": 0.0
  },
  "final_outcome": {
    "value": "The operative result: e.g. 'Penalty of PKR X imposed' | 'Stern warning issued' | 'Show Cause Notice issued' | 'Proceedings dropped' | 'Settlement accepted' | 'Compliance direction issued'. Quote the document's own wording if possible.",
    "confidence": 0.0
  }
}

Text of the adjudication order:
\"\"\"
{text}
\"\"\"
"""


# ==============================================================================
# Text location matching
# ==============================================================================

def _find_location(full_text: str, value: Any) -> dict:
    """
    Locate where an extracted value appears in full_text.
    Returns {char_start, char_end, excerpt} or {} if not found.
    """
    if not value or not full_text:
        return {}

    # Convert to search string
    if isinstance(value, list):
        if not value:
            return {}
        search = str(value[0])[:80]
    elif isinstance(value, (int, float)):
        search = str(int(value))
    else:
        search = str(value)[:80]

    search_clean = search.strip()
    if len(search_clean) < 4:
        return {}

    # Phase A: direct substring (case-insensitive)
    idx = full_text.lower().find(search_clean.lower())
    if idx >= 0:
        start = max(0, idx - 80)
        end   = min(len(full_text), idx + len(search_clean) + 80)
        return {
            "char_start": idx,
            "char_end":   idx + len(search_clean),
            "excerpt":    full_text[start:end].strip(),
        }

    # Phase B: fuzzy sliding window (for paraphrased content like secp_findings)
    anchor = search_clean[:60]
    best_ratio = 0.0
    best_pos   = -1
    win_size   = 200

    for i in range(0, len(full_text) - win_size, 50):
        window = full_text[i: i + win_size]
        ratio  = difflib.SequenceMatcher(None, anchor.lower(), window.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pos   = i

    if best_ratio >= 0.5 and best_pos >= 0:
        start = max(0, best_pos - 40)
        end   = min(len(full_text), best_pos + win_size + 40)
        return {
            "char_start": best_pos,
            "char_end":   best_pos + win_size,
            "excerpt":    full_text[start:end].strip(),
        }

    return {}


def _find_date_location(full_text: str, iso_date: Optional[str]) -> dict:
    """
    Specialised location finder for dates — tries multiple formats.
    e.g. "2025-10-16" might appear as "October 16, 2025" or "16.10.2025" or "16-10-2025"
    """
    if not iso_date:
        return {}
    try:
        from datetime import datetime
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return _find_location(full_text, iso_date)

    candidates = [
        iso_date,                                            # 2025-10-16
        dt.strftime("%d.%m.%Y"),                             # 16.10.2025
        dt.strftime("%d.%m.%y"),                             # 16.10.25
        dt.strftime("%d-%m-%Y"),                             # 16-10-2025
        dt.strftime("%B %d, %Y"),                            # October 16, 2025
        dt.strftime("%-d %B %Y") if hasattr(dt, '_') else    # 16 October 2025
            f"{dt.day} {dt.strftime('%B')} {dt.year}",
        dt.strftime("%d %B, %Y"),                            # 16 October, 2025
    ]
    for cand in candidates:
        loc = _find_location(full_text, cand)
        if loc:
            return loc
    return {}


# ==============================================================================
# Extractor
# ==============================================================================

class StructuredExtractor:

    def __init__(self):
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)
        self.db     = KnowledgeDB()

    def already_extracted(self, doc_id: str) -> bool:
        return self.db.get_order(doc_id) is not None

    def extract(self, doc: dict) -> dict:
        """
        Run GPT-4o-mini on the document's full_text.
        Returns the parsed extraction dict with {field: {value, confidence}}.
        """
        full_text = (doc.get("full_text") or "").strip()
        if not full_text:
            raise ValueError(f"No full_text in document: {doc.get('filename')}")

        truncated = full_text[:MAX_TEXT_CHARS]
        prompt    = _SCHEMA.replace("{text}", truncated)

        resp = self.client.chat.completions.create(
            model=config.STRUCTURED_EXTRACTION_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )

        return json.loads(resp.choices[0].message.content)

    def process_document(
        self,
        doc:      dict,
        force:    bool = False,
        dry_run:  bool = False,
    ) -> dict:
        """
        Full pipeline for one document:
          1. Run LLM extraction
          2. Find text locations for each field
          3. Build confidence map
          4. Write to SQLite (unless dry_run)
        Returns summary dict.
        """
        doc_id   = doc["doc_id"]
        filename = doc["filename"]

        if not force and not dry_run and self.already_extracted(doc_id):
            return {"status": "skipped", "filename": filename}

        try:
            raw = self.extract(doc)
        except Exception as exc:
            return {"status": "failed", "filename": filename, "error": str(exc)}

        full_text        = doc.get("full_text") or ""
        fields           : dict[str, Any]        = {}
        confidence_scores: dict[str, float]      = {}
        text_locations   : dict[str, dict]       = {}
        flagged_fields   : list[str]             = []

        for field in ALL_FIELDS:
            block = raw.get(field, {})
            if not isinstance(block, dict):
                # Fallback: bare value without confidence wrapper
                val  = block
                conf = 0.5
            else:
                val  = block.get("value")
                conf = float(block.get("confidence", 0.5))

            # Normalise None-like values
            if val in (None, "null", "N/A", "Not mentioned", "Not found", ""):
                val  = None
                conf = min(conf, 0.3)

            # Clamp confidence
            conf = max(0.0, min(1.0, conf))

            fields[field]            = val
            confidence_scores[field] = conf

            if conf < config.CONFIDENCE_REVIEW_THRESHOLD:
                flagged_fields.append(field)

            # Text location
            if val is not None:
                if field in ("order_date", "date_of_notice") or \
                   (field == "date_of_hearing" and isinstance(val, list) and val):
                    date_to_find = val[0] if isinstance(val, list) else val
                    loc = _find_date_location(full_text, date_to_find)
                else:
                    loc = _find_location(full_text, val)
                if loc:
                    text_locations[field] = loc

        summary = {
            "status":          "success",
            "filename":        filename,
            "flagged_count":   len(flagged_fields),
            "flagged_fields":  flagged_fields,
            "avg_confidence":  round(
                sum(confidence_scores.values()) / len(confidence_scores), 2
            ) if confidence_scores else 0.0,
        }

        if dry_run:
            summary["extraction"] = {
                f: {"value": fields[f], "confidence": confidence_scores[f]}
                for f in ALL_FIELDS
            }
            return summary

        self.db.upsert_order(
            doc_id=doc_id,
            filename=filename,
            fields=fields,
            confidence_scores=confidence_scores,
            text_locations=text_locations,
            extraction_model=config.STRUCTURED_EXTRACTION_MODEL,
        )
        return summary


# ==============================================================================
# Runner
# ==============================================================================

def process_all(
    force:           bool = False,
    dry_run:         bool = False,
    target_filename: Optional[str] = None,
    delay:           float = 0.3,
) -> None:
    doc_files = sorted(config.PROCESSED_DIR.glob("*.json"))
    if target_filename:
        doc_files = [
            f for f in doc_files
            if json.loads(f.read_text(encoding="utf-8")).get("filename") == target_filename
        ]

    print(f"\n{'='*62}")
    print(f"  SECP Phase 3 - Structured Extraction  |  {len(doc_files)} document(s)")
    if dry_run:
        print("  ** DRY RUN — no database writes **")
    print(f"{'='*62}")

    extractor = StructuredExtractor()
    counts = {"success": 0, "skipped": 0, "failed": 0}

    for doc_path in doc_files:
        doc      = json.loads(doc_path.read_text(encoding="utf-8"))
        filename = doc.get("filename", doc_path.name)

        result = extractor.process_document(doc, force=force, dry_run=dry_run)
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1

        if status == "success":
            flagged = result.get("flagged_count", 0)
            avg_c   = result.get("avg_confidence", 0)
            flag_str = f"  [{flagged} flagged]" if flagged else ""
            print(f"  [OK]  {filename[:55]:<55}  conf={avg_c:.2f}{flag_str}")

            if dry_run and result.get("extraction"):
                print()
                for field, data in result["extraction"].items():
                    val  = data["value"]
                    conf = data["confidence"]
                    flag = " <-- REVIEW" if conf < config.CONFIDENCE_REVIEW_THRESHOLD else ""
                    print(f"        {field:<28} [{conf:.2f}]{flag}")
                    if val is not None:
                        display = str(val)[:120]
                        print(f"          {display}")
                print()

        elif status == "skipped":
            print(f"  [--]  {filename[:55]:<55}  already extracted")
        else:
            print(f"  [!]   {filename[:55]:<55}  ERROR: {result.get('error','')}")

        if status != "skipped":
            time.sleep(delay)

    print(f"\n{'='*62}")
    print(f"  Summary: {counts}")

    if not dry_run:
        db = KnowledgeDB()
        st = db.stats()
        print(f"\n  Database stats:")
        print(f"    Total records   : {st['total_records']}")
        print(f"    Pending review  : {st['pending_review']}")
        print(f"    Open queue items: {st['open_queue_items']}")
        print(f"    Avg confidence  : {st['avg_confidence']:.2f}")
    print(f"{'='*62}")


# ==============================================================================
# Entry point
# ==============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="SECP Phase 3 - Structured Knowledge Extraction"
    )
    ap.add_argument("--force",    action="store_true",
                    help="Re-extract all documents (overwrite existing DB records)")
    ap.add_argument("--dry-run",  action="store_true",
                    help="Print extraction results without writing to database")
    ap.add_argument("--doc",      type=str, default=None,
                    help="Process a single document by filename")
    ap.add_argument("--stats",    action="store_true",
                    help="Show database statistics and exit")
    args = ap.parse_args()

    if args.stats:
        db = KnowledgeDB()
        st = db.stats()
        print(f"\n{'='*50}")
        print("  Knowledge Database Statistics")
        print(f"{'='*50}")
        for k, v in st.items():
            print(f"  {k:<25}: {v}")
        flagged = db.get_flagged_docs()
        if flagged:
            print(f"\n  Documents with flagged fields:")
            for d in flagged:
                print(f"    {d['filename'][:50]:<50} ({d['flagged_count']} fields, "
                      f"min conf={d['min_confidence']:.2f})")
        print(f"{'='*50}")
        return

    process_all(
        force=args.force,
        dry_run=args.dry_run,
        target_filename=args.doc,
    )


if __name__ == "__main__":
    main()
