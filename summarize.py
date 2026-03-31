import argparse
import base64
import io
import json
import re
import sys
import textwrap
from datetime import datetime
from pathlib import Path
import config

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

try:
    import pytesseract
    from pdf2image import convert_from_path
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    OCR_OK = True
except ImportError:
    OCR_OK = False

try:
    from openai import OpenAI
    _oai = OpenAI(api_key=config.OPENAI_API_KEY)
    OPENAI_OK = bool(config.OPENAI_API_KEY)
except Exception:
    _oai = None
    OPENAI_OK = False


# ══════════════════════════════════════════════════════════════════════════════
# 1. Text extraction (reuses Phase 1 approach – no ingestion required)
# ══════════════════════════════════════════════════════════════════════════════

def _vision_ocr_page(image) -> str:
    """GPT-4o Vision OCR for one page image."""
    if not OPENAI_OK:
        return ""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    resp = _oai.chat.completions.create(
        model=config.VISION_OCR_MODEL,
        messages=[
            {"role": "system", "content":
             "You are an OCR engine. Output every character visible in the "
             "image exactly as it appears. Never refuse, summarise, or explain. "
             "Raw text only."},
            {"role": "user", "content": [
                {"type": "text",
                 "text": "Output all text visible in this image, preserving "
                         "line breaks and paragraph spacing."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}",
                               "detail": "high"}},
            ]},
        ],
        max_tokens=4096,
    )
    text = resp.choices[0].message.content.strip()
    refusals = ("i'm unable", "i cannot", "i can't", "i am unable",
                "i'm sorry", "i am sorry", "cannot assist", "can't assist")
    if any(r in text.lower() for r in refusals):
        return ""
    return text


def extract_text(pdf_path: Path) -> str:
    """
    Extract full text from a PDF using pdfplumber + best-of-both OCR
    (Tesseract vs GPT-4o Vision) for scanned pages.
    Returns the concatenated plain text of all pages.
    """
    if not PDFPLUMBER_OK:
        raise RuntimeError("pdfplumber is not installed.")

    MIN_CHARS = config.MIN_CHARS_PER_PAGE
    pages_text = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        raw = [(i + 1, (p.extract_text() or "").strip())
               for i, p in enumerate(pdf.pages)]

    scanned_page_nums = [pn for pn, t in raw if len(t) < MIN_CHARS]

    # Convert scanned pages to images
    ocr_images: dict[int, object] = {}
    if scanned_page_nums and OCR_OK:
        try:
            imgs = convert_from_path(
                str(pdf_path),
                dpi=config.OCR_DPI,
                first_page=min(scanned_page_nums),
                last_page=max(scanned_page_nums),
                poppler_path=config.POPPLER_PATH or None,
            )
            for img, pn in zip(imgs, range(min(scanned_page_nums),
                                           max(scanned_page_nums) + 1)):
                if pn in scanned_page_nums:
                    ocr_images[pn] = img
        except Exception:
            pass

    for page_num, plumber_text in raw:
        if page_num in ocr_images and len(plumber_text) < MIN_CHARS:
            # Best-of-both: Tesseract vs Vision OCR
            tess_text = ""
            if OCR_OK:
                try:
                    tess_text = pytesseract.image_to_string(
                        ocr_images[page_num], lang="eng", config="--psm 3"
                    ).strip()
                except Exception:
                    pass

            vision_text = _vision_ocr_page(ocr_images[page_num]) if OPENAI_OK else ""

            page_text = vision_text if len(vision_text) >= len(tess_text) else tess_text
        else:
            page_text = plumber_text

        pages_text.append(page_text)

    return "\n\n".join(pages_text)


# 2. Structured extraction via GPT-4o-mini
# ════════════════════════════════════════

_EXTRACTION_SYSTEM = """You are a legal document analyst for SECP (Securities and Exchange
Commission of Pakistan). Extract structured information from adjudication orders.
Rules:
- Present only facts as stated in the document. Do not interpret or opine.
- Use neutral, formal language.
- If a field is not present in the document, return null."""

_EXTRACTION_PROMPT = """\
Extract the following fields from this SECP adjudication order and return valid JSON only.

{{
  "order_reference":        "<official reference/case number or null>",
  "entity_names":           ["<company or individual name>"],
  "individual_respondents": ["<name, designation>"],
  "issuing_officer":        "<name and designation or null>",
  "order_date":             "<YYYY-MM-DD or null>",
  "date_of_notice":         "<YYYY-MM-DD or null>",
  "date_of_hearing":        ["<YYYY-MM-DD>"],
  "entity_category":        "<Listed Company | Unlisted Company | Auditor | Individual | Other>",
  "sector":                 "<Corporate | Securities | Insurance | NBFC | Other>",
  "violations":             ["<concise description>"],
  "legal_provisions":       [{{"section": "<>", "act": "<>", "clause": "<or null>"}}],
  "penalty_pkr":            "<number or null>",
  "penalty_note":           "<exact wording of penalty clause or null>",
  "action_types":           ["<Penalty | Warning | Direction | Suspension | Cancellation | Other>"],
  "key_facts":              ["<material fact>"],
  "case_summary":           "<2-3 sentence background>",
  "secp_findings":          "<Commission findings and reasoning>",
  "final_outcome":          "<exact outcome as stated in order>"
}}

DOCUMENT TEXT:
\"\"\"
{text}
\"\"\""""


def extract_structured(full_text: str) -> dict:
    """Call GPT-4o-mini to extract structured fields from document text."""
    if not OPENAI_OK:
        raise RuntimeError("OpenAI API key not set. Cannot extract structured data.")

    # Truncate to avoid token limits (~120k chars ≈ 30k tokens, well within limits)
    text_chunk = full_text[:120_000]

    resp = _oai.chat.completions.create(
        model=config.METADATA_MODEL,
        messages=[
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {"role": "user",   "content": _EXTRACTION_PROMPT.format(text=text_chunk)},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content)

# 3. Build the 9-component summary from structured data
# ═════════════════════════════════════════════════════

def _fmt_date(d: str | None) -> str:
    if not d:
        return "Not stated"
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%-d %B %Y")
    except Exception:
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d %B %Y")
        except Exception:
            return d


def _fmt_penalty(pkr, note, actions) -> str:
    actions = actions or []
    if pkr and pkr > 0:
        base = f"Monetary penalty of PKR {pkr:,.0f} imposed."
        return f"{base} {note}" if note else base
    if pkr == -1:
        return note or "Penal action as prescribed under applicable sections."
    non_monetary = [a for a in actions if a not in ("Penalty",)]
    if non_monetary:
        return f"Action(s) taken: {', '.join(non_monetary)}."
    if note:
        return note
    return "No monetary penalty imposed."


def _fmt_provisions(provisions: list) -> str:
    if not provisions:
        return "Not specified."
    lines = []
    for p in provisions:
        sec  = p.get("section") or ""
        act  = p.get("act") or ""
        cl   = p.get("clause") or ""
        part = f"Section {sec}" if sec else ""
        if cl:
            part += f"({cl})"
        if act:
            part += f" of {act}" if part else act
        if part:
            lines.append(part)
    return "\n    ".join(f"- {l}" for l in lines) if lines else "Not specified."


def build_summary(data: dict, source_path: Path | None = None) -> dict:
    """
    Assemble the 9-component summary dict from extracted structured data.
    All components are derived from the structured data — no additional LLM call.
    """
    entities   = data.get("entity_names") or []
    respondent = "; ".join(entities) if entities else "Not stated"
    officer    = data.get("issuing_officer") or "Not stated"
    ref        = data.get("order_reference") or "Not stated"
    odate      = _fmt_date(data.get("order_date"))
    laws       = list({p.get("act") for p in (data.get("legal_provisions") or [])
                       if p.get("act")})
    laws_str   = "; ".join(laws) if laws else "Not stated"

    # 1. Case Header
    case_header = (
        f"Reference     : {ref}\n"
        f"Respondent(s) : {respondent}\n"
        f"Order Date    : {odate}\n"
        f"Law(s) Applied: {laws_str}\n"
        f"Issuing Authority: {officer}"
    )

    # 2. Case Background
    case_background = data.get("case_summary") or "Not available in this order."

    # 3. Key Facts
    key_facts = data.get("key_facts") or []

    # 4. Violation Identified
    violations = data.get("violations") or []
    violation_identified = (
        "\n    ".join(f"- {v}" for v in violations)
        if violations else "Not described in this order."
    )

    # 5. Legal Provisions Applied
    legal_provisions_applied = _fmt_provisions(data.get("legal_provisions") or [])

    # 6. SECP's Determination
    secp_determination = (
        data.get("secp_findings") or
        data.get("case_summary") or
        "Not available in this order."
    )

    # 7. Penalty or Sanction
    penalty_or_sanction = _fmt_penalty(
        data.get("penalty_pkr"),
        data.get("penalty_note"),
        data.get("action_types"),
    )

    # 8. Source Citation
    fname = source_path.name if source_path else "Unknown"
    notice_date  = _fmt_date(data.get("date_of_notice"))
    hearing_dates = data.get("date_of_hearing") or []
    hearing_str  = ("; ".join(_fmt_date(d) for d in hearing_dates)
                    if hearing_dates else "Not stated")
    source_citation = (
        f"Order Reference    : {ref}\n"
        f"Respondent         : {respondent}\n"
        f"Order Date         : {odate}\n"
        f"Notice Date        : {notice_date}\n"
        f"Hearing Date(s)    : {hearing_str}\n"
        f"Source File        : {fname}\n"
        f"Repository         : secp.gov.pk > Document Center > Adjudication Orders"
    )

    # 9. Current Status
    outcome = data.get("final_outcome")
    current_status = outcome if outcome else "No subsequent status recorded in this order."

    return {
        "case_header":              case_header,
        "case_background":          case_background,
        "key_facts":                key_facts,
        "violation_identified":     violation_identified,
        "legal_provisions_applied": legal_provisions_applied,
        "secp_determination":       secp_determination,
        "penalty_or_sanction":      penalty_or_sanction,
        "source_citation":          source_citation,
        "current_status":           current_status,
        "_meta": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "source_file":  str(source_path) if source_path else None,
            "respondent":   respondent,
            "order_date":   data.get("order_date"),
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. Multi-case consolidated summary
# ══════════════════════════════════════════════════════════════════════════════

_MULTI_SYSTEM = """You are a senior analyst at SECP. Produce a neutral, factual
consolidated summary of multiple adjudication orders.
Rules:
- Present only facts supported by the underlying orders.
- No legal interpretation or opinion.
- Preserve individual case references.
- State the scope, period, and number of orders clearly."""

_MULTI_PROMPT = """\
Produce a consolidated summary of the {n} SECP adjudication order(s) below.

Return JSON with this structure:
{{
  "scope_statement": "<one sentence: scope, time period, count>",
  "introduction":    "<1-2 sentences overview>",
  "patterns": [
    {{"pattern": "<factual pattern>", "supporting_cases": ["<ref1>", "<ref2>"]}}
  ],
  "aggregate_stats": {{
    "total_monetary_penalties_pkr": <number or null>,
    "most_cited_sections": ["<sec>"],
    "most_common_action": "<action>"
  }},
  "individual_cases": [
    {{"reference": "<ref>", "respondent": "<name>", "outcome": "<brief>", "date": "<YYYY-MM-DD>"}}
  ],
  "conclusion": "<1-2 sentence factual wrap-up>"
}}

CASES:
{cases_json}"""


def multi_case_summary(summaries: list[dict], structured_list: list[dict],
                        scope_desc: str = "") -> dict:
    """Generate a consolidated summary from multiple case summaries."""
    if not OPENAI_OK:
        raise RuntimeError("OpenAI API key required for multi-case summaries.")

    cases_payload = []
    for s, d in zip(summaries, structured_list):
        cases_payload.append({
            "order_reference":   d.get("order_reference", "N/A"),
            "respondent":        "; ".join(d.get("entity_names") or []),
            "order_date":        d.get("order_date"),
            "violations":        d.get("violations", []),
            "legal_provisions":  d.get("legal_provisions", []),
            "penalty_pkr":       d.get("penalty_pkr"),
            "action_types":      d.get("action_types", []),
            "final_outcome":     d.get("final_outcome", ""),
            "secp_findings":     (d.get("secp_findings") or "")[:500],
        })

    resp = _oai.chat.completions.create(
        model=config.QUERY_PARSE_MODEL,
        messages=[
            {"role": "system", "content": _MULTI_SYSTEM},
            {"role": "user",   "content": _MULTI_PROMPT.format(
                n=len(cases_payload),
                cases_json=json.dumps(cases_payload, indent=2),
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    result = json.loads(resp.choices[0].message.content)
    result["_meta"] = {
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "scope":          scope_desc,
        "document_count": len(summaries),
    }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 5. Display helpers
# ══════════════════════════════════════════════════════════════════════════════

_W = 70   # display width

def _hr(char="-"):    print(char * _W)
def _banner(title):   print("=" * _W); print(f"  {title}"); print("=" * _W)
def _section(title):  print(); print(f"  --- {title.upper()} {'-'*(max(0,_W-6-len(title)))}"); print()


def display_summary(s: dict):
    meta = s.get("_meta", {})
    _banner(f"CASE SUMMARY — {meta.get('respondent', 'Unknown')}")

    _section("Case Header")
    for line in s["case_header"].splitlines():
        print(f"  {line}")

    _section("Case Background")
    for line in textwrap.wrap(s["case_background"], width=_W - 4):
        print(f"  {line}")

    _section("Key Facts")
    facts = s["key_facts"]
    if facts:
        for i, f in enumerate(facts, 1):
            wrapped = textwrap.wrap(f, width=_W - 7)
            print(f"  {i}. {wrapped[0]}")
            for cont in wrapped[1:]:
                print(f"     {cont}")
    else:
        print("  Not listed separately in this order.")

    _section("Violation Identified")
    for line in s["violation_identified"].splitlines():
        print(f"  {line}")

    _section("Legal Provisions Applied")
    for line in s["legal_provisions_applied"].splitlines():
        print(f"  {line}")

    _section("SECP's Determination")
    for line in textwrap.wrap(s["secp_determination"], width=_W - 4):
        print(f"  {line}")

    _section("Penalty / Sanction")
    for line in textwrap.wrap(s["penalty_or_sanction"], width=_W - 4):
        print(f"  {line}")

    _section("Source Citation")
    for line in s["source_citation"].splitlines():
        print(f"  {line}")

    _section("Current Status")
    for line in textwrap.wrap(s["current_status"], width=_W - 4):
        print(f"  {line}")

    _hr("=")
    gen = meta.get("generated_at", "")[:19].replace("T", " ")
    print(f"  Generated: {gen} UTC")
    print()


def display_multi_summary(m: dict):
    meta  = m.get("_meta", {})
    scope = meta.get("scope", "")
    n     = meta.get("document_count", 0)
    _banner(f"CONSOLIDATED SUMMARY -- {n} ORDER(S)  {scope}")

    print(f"\n  Scope : {m.get('scope_statement','')}")

    _section("Introduction")
    for line in textwrap.wrap(m.get("introduction", ""), width=_W - 4):
        print(f"  {line}")

    stats = m.get("aggregate_stats", {})
    if stats:
        _section("Aggregate Statistics")
        total = stats.get("total_monetary_penalties_pkr")
        if total:
            print(f"  Total monetary penalties : PKR {total:,.0f}")
        sections = stats.get("most_cited_sections", [])
        if sections:
            print(f"  Most cited sections      : {', '.join(sections)}")
        action = stats.get("most_common_action")
        if action:
            print(f"  Most common action       : {action}")

    patterns = m.get("patterns", [])
    if patterns:
        _section("Patterns Identified")
        for p in patterns:
            wrapped = textwrap.wrap(p.get("pattern", ""), width=_W - 7)
            cases   = p.get("supporting_cases", [])
            print(f"  - {wrapped[0]}")
            for cont in wrapped[1:]:
                print(f"    {cont}")
            if cases:
                print(f"    [{', '.join(cases)}]")

    cases = m.get("individual_cases", [])
    if cases:
        _section("Individual Cases")
        for c in cases:
            print(f"  [{c.get('reference','N/A')}]  {c.get('respondent','')}  —  {c.get('date','')}")
            out = c.get("outcome", "")
            if out:
                for line in textwrap.wrap(out, width=_W - 6):
                    print(f"    {line}")

    _section("Conclusion")
    for line in textwrap.wrap(m.get("conclusion", ""), width=_W - 4):
        print(f"  {line}")

    _hr("=")
    gen = meta.get("generated_at", "")[:19].replace("T", " ")
    print(f"  Generated: {gen} UTC")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# 6. Pipeline: PDF → summary
# ══════════════════════════════════════════════════════════════════════════════

def summarize_pdf(pdf_path: Path, fmt: str = "text", out_path: Path | None = None):
    """Full pipeline: extract text → extract structure → build summary → display."""
    path = Path(pdf_path)
    if not path.exists():
        print(f"[ERROR] File not found: {path}", file=sys.stderr)
        sys.exit(1)
    if path.suffix.lower() != ".pdf":
        print(f"[ERROR] Only PDF files are supported.", file=sys.stderr)
        sys.exit(1)

    print(f"  Extracting text from {path.name}...", end=" ", flush=True)
    full_text = extract_text(path)
    print(f"{len(full_text):,} chars")

    if len(full_text.strip()) < 100:
        print("[ERROR] Could not extract readable text from this PDF.", file=sys.stderr)
        sys.exit(1)

    print("  Extracting structured data (GPT-4o-mini)...", end=" ", flush=True)
    data = extract_structured(full_text)
    print("done")

    print("  Building summary...", end=" ", flush=True)
    summary = build_summary(data, source_path=path)
    summary["_structured"] = data   # keep raw data for JSON output
    print("done\n")

    if fmt == "json":
        output = json.dumps(summary, indent=2, ensure_ascii=False)
        if out_path:
            out_path.write_text(output, encoding="utf-8")
            print(f"Saved to {out_path}")
        else:
            print(output)
    else:
        display_summary(summary)
        if out_path:
            lines = []
            _orig_print = __builtins__["print"] if isinstance(__builtins__, dict) else print
            # Simple text capture for file output
            with open(out_path, "w", encoding="utf-8") as f:
                _write_text_summary(summary, f)
            print(f"Saved to {out_path}")

    return summary


def _write_text_summary(s: dict, f):
    """Write plain-text summary to file object."""
    meta = s.get("_meta", {})
    sections = [
        ("CASE HEADER",              s["case_header"]),
        ("CASE BACKGROUND",          s["case_background"]),
        ("VIOLATION IDENTIFIED",     s["violation_identified"]),
        ("LEGAL PROVISIONS APPLIED", s["legal_provisions_applied"]),
        ("SECP'S DETERMINATION",     s["secp_determination"]),
        ("PENALTY / SANCTION",       s["penalty_or_sanction"]),
        ("SOURCE CITATION",          s["source_citation"]),
        ("CURRENT STATUS",           s["current_status"]),
    ]
    f.write(f"CASE SUMMARY — {meta.get('respondent','')}\n")
    f.write("=" * 70 + "\n\n")

    f.write("KEY FACTS\n" + "─" * 70 + "\n")
    for i, fact in enumerate(s.get("key_facts") or [], 1):
        f.write(f"  {i}. {fact}\n")
    f.write("\n")

    for title, body in sections:
        f.write(f"{title}\n" + "─" * 70 + "\n")
        f.write(f"  {body}\n\n")


# ══════════════════════════════════════════════════════════════════════════════
# 7. Multi-PDF pipeline
# ══════════════════════════════════════════════════════════════════════════════

def summarize_multi(pdf_paths: list[Path], scope_desc: str = "",
                    fmt: str = "text", out_path: Path | None = None):
    """Summarize multiple PDFs and generate a consolidated summary."""
    all_summaries = []
    all_structured = []

    for path in pdf_paths:
        p = Path(path)
        print(f"\n  [{p.name}]")
        print(f"    Extracting text...", end=" ", flush=True)
        full_text = extract_text(p)
        print(f"{len(full_text):,} chars")
        print(f"    Extracting structure...", end=" ", flush=True)
        data = extract_structured(full_text)
        print("done")
        summary = build_summary(data, source_path=p)
        all_summaries.append(summary)
        all_structured.append(data)

    print(f"\n  Generating consolidated summary ({len(pdf_paths)} orders)...",
          end=" ", flush=True)
    multi = multi_case_summary(all_summaries, all_structured, scope_desc)
    print("done\n")

    if fmt == "json":
        output = json.dumps({
            "consolidated": multi,
            "individual":   all_summaries,
        }, indent=2, ensure_ascii=False)
        if out_path:
            out_path.write_text(output, encoding="utf-8")
            print(f"Saved to {out_path}")
        else:
            print(output)
    else:
        display_multi_summary(multi)
        print("\n" + "─" * 70)
        print("  INDIVIDUAL SUMMARIES")
        print("─" * 70)
        for s in all_summaries:
            display_summary(s)

    return multi, all_summaries


# ══════════════════════════════════════════════════════════════════════════════
# 8. CLI
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="SECP Adjudication Order Summarizer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python summarize.py order.pdf
              python summarize.py order.pdf --format json
              python summarize.py order.pdf --out summary.txt
              python summarize.py --multi doc1.pdf doc2.pdf --scope "Section 510 orders, 2025"
        """),
    )
    parser.add_argument("pdf", nargs="?", help="Path to a single PDF file")
    parser.add_argument(
        "--multi", nargs="+", metavar="PDF",
        help="Two or more PDF files for a consolidated summary",
    )
    parser.add_argument(
        "--scope", default="",
        help="Human-readable scope description for multi-case summaries",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--out", metavar="FILE",
        help="Save output to this file instead of printing to terminal",
    )

    args = parser.parse_args()

    if not OPENAI_OK:
        print("[ERROR] OPENAI_API_KEY is not set. "
              "Add it to your .env file and retry.", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else None

    if args.multi:
        paths = [Path(p) for p in args.multi]
        missing = [p for p in paths if not p.exists()]
        if missing:
            print(f"[ERROR] Files not found: {missing}", file=sys.stderr)
            sys.exit(1)
        print(f"\n{'='*70}")
        print(f"  SECP Summarizer  |  {len(paths)} document(s)")
        print(f"{'='*70}")
        summarize_multi(paths, scope_desc=args.scope,
                        fmt=args.format, out_path=out_path)

    elif args.pdf:
        print(f"\n{'='*70}")
        print(f"  SECP Summarizer  |  {Path(args.pdf).name}")
        print(f"{'='*70}\n")
        summarize_pdf(Path(args.pdf), fmt=args.format, out_path=out_path)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
