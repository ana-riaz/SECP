"""
Phase 2, Step 1 – Structured Metadata Extraction
=================================================
Uses OpenAI GPT-4o-mini to extract structured legal metadata from each
processed document's full_text. Results are saved back into the
processed_docs JSON files as an 'extracted_metadata' field.

Also generates and stores a 9-component structured_summary for each document.

Fields extracted
────────────────
  order_reference         : official order number / reference string
  entity_names            : list of company/entity respondents
  individual_respondents  : list of named individuals (directors, CAs, auditors)
  entity_category         : "Listed Company" | "Unlisted Company" | ...
  sector                  : industry sector
  violations              : list of specific violation descriptions
  legal_provisions        : [{section, act, clause}]
  penalty_pkr             : total monetary penalty (float) or null
  penalty_note            : description if non-monetary action
  issuing_officer         : name + designation of signing authority
  action_types            : ["Penalty", "Warning", "Show Cause Notice", ...]
  order_date              : "YYYY-MM-DD" or null
  case_summary            : 2-3 sentence summary
  key_facts               : list of 3-5 key facts

Usage
─────
  python extract_metadata.py                    # skip already-extracted docs
  python extract_metadata.py --force            # re-extract everything
  python extract_metadata.py --doc <file>       # single document by filename
  python extract_metadata.py --generate-summaries  # backfill summaries only
"""

from __future__ import annotations

import argparse
import json
import re
import time
import math
from pathlib import Path
from typing import Optional

from openai import OpenAI, RateLimitError

import config

# Tokens per minute budget for gpt-4o-mini
_TPM_LIMIT   = 200_000
_TOKENS_PER_CHAR = 0.25   # rough estimate: 1 token ≈ 4 chars


# ══════════════════════════════════════════════════════════════════════════════
# Prompts
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a legal analyst specialising in SECP (Securities and Exchange Commission
of Pakistan) adjudication orders under the Companies Act 2017 and related laws.

Your task is to extract structured information from the order text provided.
Return ONLY a valid JSON object. Do NOT include any explanation or markdown fences.
Extract only information explicitly present in the text — never invent or infer."""

EXTRACTION_SCHEMA = """{
  "order_reference": "string — official order/case reference number, e.g. 'Order No. dated 20-10-2025' or 'SCN No. Adj-I/...'",
  "entity_names": ["list of company or entity names that are respondents/noticees"],
  "individual_respondents": ["list of individual names with designation, e.g. 'Mr. John Smith, CEO'"],
  "entity_category": "one of: Listed Company | Unlisted Company | Broker | Asset Management Company | NBFC | Insurance Company | Other",
  "sector": "industry sector, e.g. Textile | Cement | Pharmaceuticals | Fertilizer | Iron & Steel | Financial Services | Other",
  "violations": ["list of specific violation descriptions as concise phrases"],
  "legal_provisions": [{"section": "section number as string", "act": "full act name", "clause": "sub-clause or null"}],
  "penalty_pkr": null or numeric total penalty in PKR (number only, no currency symbols),
  "penalty_note": "description of penalty/action if no simple monetary amount, else null",
  "issuing_officer": "full name and designation of the signatory authority, or null",
  "action_types": ["list of: Penalty | Warning | Show Cause Notice | Settlement | License Suspension | License Cancellation | Compliance Direction | Other"],
  "order_date": "YYYY-MM-DD or null",
  "case_summary": "2-3 sentence summary of the case facts and outcome",
  "key_facts": ["3-5 key facts about the case"]
}"""

USER_PROMPT_TEMPLATE = """Extract structured metadata from this SECP adjudication order.

Return a JSON object matching this schema exactly:
{schema}

Order text:
\"\"\"
{text}
\"\"\"
"""

# ── Summary-specific additional fields ────────────────────────────────────────

SUMMARY_EXTRA_SCHEMA = """{
  "secp_findings": "2-4 sentence neutral summary of the Commission's findings, reasoning, and determination as recorded in the decision paragraphs",
  "final_outcome": "Exact outcome statement as written in the final section of the order (penalty text, warning text, direction, etc.)",
  "date_of_notice": "YYYY-MM-DD date of the Show Cause Notice, or null",
  "date_of_hearing": ["YYYY-MM-DD dates of hearings held, in chronological order"]
}"""

SUMMARY_EXTRA_PROMPT = """Extract the following additional fields from this SECP adjudication order.
Return ONLY a valid JSON object matching this schema exactly:
{schema}

Order text:
\"\"\"
{text}
\"\"\""""


# ══════════════════════════════════════════════════════════════════════════════
# Regex fallback (when OpenAI unavailable)
# ══════════════════════════════════════════════════════════════════════════════

def _regex_extract(text: str, filename_meta: dict) -> dict:
    """Best-effort regex extraction when LLM is unavailable."""

    # Penalty amounts: look for PKR / Rs. followed by numbers
    penalty = None
    m = re.search(r'(?:PKR|Rs\.?)\s*([\d,]+(?:\.\d+)?)\s*(?:million|lakh)?', text, re.IGNORECASE)
    if m:
        raw = m.group(1).replace(',', '')
        mult = 1_000_000 if 'million' in m.group(0).lower() else (100_000 if 'lakh' in m.group(0).lower() else 1)
        try:
            penalty = float(raw) * mult
        except ValueError:
            pass

    # Section numbers from text (e.g., "section 134", "Section 510")
    sections = list(dict.fromkeys(re.findall(r'[Ss]ection\s+(\d+[A-Z]?)', text)))

    # Date
    order_date = filename_meta.get('order_date_iso')

    # Entity from filename
    entity = filename_meta.get('company_name', '')

    return {
        "order_reference": None,
        "entity_names": [entity] if entity else [],
        "individual_respondents": [],
        "entity_category": "Listed Company",
        "sector": "Other",
        "violations": [],
        "legal_provisions": [{"section": s, "act": "Companies Act 2017", "clause": None} for s in sections],
        "penalty_pkr": penalty,
        "penalty_note": None,
        "issuing_officer": None,
        "action_types": ["Penalty"] if penalty else [],
        "order_date": order_date,
        "case_summary": text[:500] if text else "",
        "key_facts": [],
        "_extraction_method": "regex_fallback",
    }


# ══════════════════════════════════════════════════════════════════════════════
# LLM extractor
# ══════════════════════════════════════════════════════════════════════════════

class MetadataExtractor:

    # Truncate to ~28k chars to stay within token limits (≈7k tokens for gpt-4o-mini)
    MAX_TEXT_CHARS = 28_000

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise ValueError(
                "OPENAI_API_KEY is not set. "
                "Add it to .env or use --regex-only flag for fallback extraction."
            )
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)

    def extract(self, full_text: str, max_retries: int = 5) -> dict:
        text = full_text[:self.MAX_TEXT_CHARS]
        prompt = USER_PROMPT_TEMPLATE.format(schema=EXTRACTION_SCHEMA, text=text)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ]

        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=config.EXTRACTION_MODEL,
                    response_format={"type": "json_object"},
                    messages=messages,
                    temperature=0,
                )
                raw    = response.choices[0].message.content
                result = json.loads(raw)
                result["_extraction_method"] = "llm"
                result["_model"] = config.EXTRACTION_MODEL
                return result

            except RateLimitError:
                if attempt == max_retries - 1:
                    raise
                wait = min(60, 5 * (2 ** attempt))   # 5s, 10s, 20s, 40s, 60s
                print(f"    [rate-limit] waiting {wait}s (attempt {attempt+1}/{max_retries})…")
                time.sleep(wait)


# ══════════════════════════════════════════════════════════════════════════════
# Summary generator
# ══════════════════════════════════════════════════════════════════════════════

def generate_doc_summary(full_text: str, metadata: dict, filename: str,
                         client: OpenAI, max_retries: int = 5) -> dict:
    """
    Generate the 9-component structured summary for a document.
    Makes one additional LLM call to extract secp_findings, final_outcome,
    date_of_notice, date_of_hearing, then calls build_summary() from summarize.py.
    """
    from summarize import build_summary
    from pathlib import Path

    text = full_text[:28_000]
    prompt = SUMMARY_EXTRA_PROMPT.format(schema=SUMMARY_EXTRA_SCHEMA, text=text)

    extra: dict = {}
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=config.EXTRACTION_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0,
            )
            extra = json.loads(response.choices[0].message.content)
            break
        except RateLimitError:
            if attempt == max_retries - 1:
                raise
            wait = min(60, 5 * (2 ** attempt))
            print(f"    [rate-limit] waiting {wait}s (attempt {attempt+1}/{max_retries})…")
            time.sleep(wait)
        except Exception:
            break  # non-retryable — proceed with empty extra

    # Merge existing metadata with extra summary fields
    combined = {**metadata, **extra}

    return build_summary(combined, source_path=Path(filename))


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ══════════════════════════════════════════════════════════════════════════════

def process_all(force: bool = False, target_filename: Optional[str] = None,
                regex_only: bool = False,
                summaries_only: bool = False) -> dict[str, str]:
    """
    Extract metadata (and structured summaries) for all processed documents.

    Returns {filename: status} where status ∈ {extracted, skipped, failed}
    """
    extractor = None
    if not regex_only:
        try:
            extractor = MetadataExtractor()
        except ValueError as e:
            print(f"[WARN] {e}")
            print("[WARN] Falling back to regex extraction for all documents.")

    import mongo_store
    all_docs = mongo_store.get_all_docs()
    if target_filename:
        all_docs = [d for d in all_docs if d.get('filename') == target_filename]
        if not all_docs:
            print(f"[ERROR] No document found in MongoDB for filename: {target_filename}")
            return {}

    results: dict[str, str] = {}

    print(f"\n{'-'*60}")
    print(f"  Metadata Extractor  |  {len(all_docs)} document(s)")
    if summaries_only:
        print(f"  Mode: summary generation only (backfill)")
    print(f"{'-'*60}")

    for doc in all_docs:
        filename = doc['filename']
        has_metadata = 'extracted_metadata' in doc
        has_summary  = 'structured_summary' in doc

        # In summaries_only mode: only generate summaries for docs that have
        # metadata but no summary yet
        if summaries_only:
            if not has_metadata:
                print(f"  [!]  {filename[:60]}  no metadata yet — run without --generate-summaries first")
                results[filename] = 'skipped_no_metadata'
                continue
            if has_summary and not force:
                print(f"  [->] {filename[:60]}  summary exists, skipped")
                results[filename] = 'skipped'
                continue
            # Generate summary from existing metadata
            full_text = doc.get('full_text', '').strip()
            meta = doc['extracted_metadata']
            try:
                summary = generate_doc_summary(full_text, meta, filename, extractor.client)
                mongo_store.update_doc_field(doc['doc_id'], 'structured_summary', summary)
                print(f"  [OK] {filename[:60]}  summary generated")
                results[filename] = 'summary_generated'
                estimated_tokens = len(full_text[:28_000]) * _TOKENS_PER_CHAR
                wait_s = max(1.5, estimated_tokens / (_TPM_LIMIT / 60))
                time.sleep(wait_s)
            except Exception as exc:
                print(f"  [X]  {filename[:60]}  FAILED: {exc}")
                results[filename] = 'failed'
            continue

        # Normal mode: skip if both metadata and summary already exist
        if not force and has_metadata and has_summary:
            print(f"  [->] {filename[:60]}  skipped")
            results[filename] = 'skipped'
            continue

        full_text = doc.get('full_text', '').strip()
        if not full_text:
            print(f"  [!]  {filename[:60]}  no text – regex only")
            meta = _regex_extract('', doc.get('order_metadata', {}))
            mongo_store.update_doc_field(doc['doc_id'], 'extracted_metadata', meta)
            results[filename] = 'extracted_regex'
            continue

        try:
            # ── Step 1: metadata extraction ──────────────────────────────────
            if not has_metadata or force:
                if extractor:
                    meta = extractor.extract(full_text)
                    estimated_tokens = len(full_text[:extractor.MAX_TEXT_CHARS]) * _TOKENS_PER_CHAR
                    wait_s = max(1.5, estimated_tokens / (_TPM_LIMIT / 60))
                    time.sleep(wait_s)
                else:
                    meta = _regex_extract(full_text, doc.get('order_metadata', {}))
                mongo_store.update_doc_field(doc['doc_id'], 'extracted_metadata', meta)
                method = 'llm' if extractor else 'regex'
                print(f"  [OK] {filename[:60]}  metadata extracted ({method})")
            else:
                meta = doc['extracted_metadata']

            # ── Step 2: structured summary ────────────────────────────────────
            if (not has_summary or force) and extractor:
                try:
                    summary = generate_doc_summary(full_text, meta, filename, extractor.client)
                    mongo_store.update_doc_field(doc['doc_id'], 'structured_summary', summary)
                    estimated_tokens = len(full_text[:28_000]) * _TOKENS_PER_CHAR
                    wait_s = max(1.5, estimated_tokens / (_TPM_LIMIT / 60))
                    time.sleep(wait_s)
                    print(f"       {filename[:60]}  summary stored")
                except Exception as exc:
                    print(f"  [!]  {filename[:60]}  summary failed: {exc}")

            results[filename] = 'extracted'

        except Exception as exc:
            print(f"  [X]  {filename[:60]}  FAILED: {exc}")
            results[filename] = 'failed'

    counts = {}
    for v in results.values():
        counts[v] = counts.get(v, 0) + 1
    print(f"\n  Summary: {counts}")
    print(f"{'-'*60}\n")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SECP Metadata Extractor – Phase 2 Step 1")
    parser.add_argument("--force",              action="store_true", help="Re-extract all documents")
    parser.add_argument("--regex-only",         action="store_true", help="Use regex fallback, skip LLM")
    parser.add_argument("--doc",                type=str, default=None,
                        help="Extract only this filename (e.g. Order-510-...pdf)")
    parser.add_argument("--generate-summaries", action="store_true",
                        help="Generate structured_summary for docs that have metadata but no summary")
    args = parser.parse_args()

    process_all(force=args.force, target_filename=args.doc, regex_only=args.regex_only,
                summaries_only=args.generate_summaries)
