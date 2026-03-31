"""
synthesis.py — LLM-powered narrative synthesis for SECP search results.

Used when search_intent == "summarize":
  Groups results into violation themes, cites examples, summarises penalties.
"""

from __future__ import annotations
import json
import config

_SUMMARIZE_SYSTEM = """\
You are a research analyst synthesising SECP (Securities and Exchange Commission \
of Pakistan) adjudication order findings. Given a user question and a set of \
structured adjudication order records, produce a grouped thematic analysis.

RULES:
- Only report facts that are explicitly present in the provided records.
- Never speculate or infer beyond the records.
- Use neutral, factual, professional language.
- Group findings by violation theme — not by individual case.
- Always cite at least one specific order as an example per theme.
- If fewer than 2 records are provided, produce a single-case detail instead of themes.

Return ONLY valid JSON matching this schema:
{
  "intro":  "<one sentence: Based on analysis of N adjudication orders involving …>",
  "themes": [
    {
      "title":        "<theme name>",
      "count":        <integer — number of orders in this theme>,
      "bullets":      ["<key finding>", "…"],
      "example_title":"<Order dated DD MMM YYYY against Entity Name>",
      "example_url":  "<source_url from records or null>"
    }
  ],
  "legal_provisions": ["<Act Section — brief description>"],
  "penalty_summary":  "<e.g. Range: PKR 50,000 to PKR 500,000. Additional: …>",
  "note": "This summary is based on adjudication orders published on secp.gov.pk. Individual case details are available in the source documents."
}\
"""


def _result_to_text(r: dict) -> str:
    lines = []
    entities = ", ".join(r.get("entity_names") or [])
    if entities:
        lines.append(f"Entity: {entities}")
    if r.get("order_date"):
        lines.append(f"Date: {r['order_date']}")
    if r.get("order_reference"):
        lines.append(f"Reference: {r['order_reference']}")
    if r.get("issuing_officer"):
        lines.append(f"Officer: {r['issuing_officer']}")
    provs = r.get("legal_provisions") or []
    if provs:
        prov_str = "; ".join(
            f"Section {p.get('section','?')} of {p.get('act','?')}"
            for p in provs if p.get("section") or p.get("act")
        )
        if prov_str:
            lines.append(f"Provisions: {prov_str}")
    viols = r.get("violations") or []
    if viols:
        lines.append(f"Violations: {'; '.join(viols[:3])}")
    if r.get("penalty_display") and r["penalty_display"] != "Not specified":
        lines.append(f"Penalty: {r['penalty_display']}")
    if r.get("action_types"):
        lines.append(f"Actions: {', '.join(r['action_types'])}")
    if r.get("case_summary"):
        lines.append(f"Summary: {r['case_summary'][:300]}")
    if r.get("source_url"):
        lines.append(f"URL: {r['source_url']}")
    return "\n".join(lines)


def synthesize_summary(query: str, results: list[dict]) -> dict | None:
    """
    Call OpenAI to synthesise a grouped thematic summary from search results.
    Returns the parsed JSON dict, or None if synthesis fails.
    """
    if not config.OPENAI_API_KEY or not results:
        return None

    records_text = "\n\n---\n\n".join(
        f"Record {i+1}:\n{_result_to_text(r)}"
        for i, r in enumerate(results[:30])   # cap at 30 to stay within context
    )
    user_content = (
        f"User query: {query}\n\n"
        f"Adjudication order records ({len(results)} total):\n\n{records_text}"
    )

    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=config.QUERY_PARSE_MODEL,
            messages=[
                {"role": "system", "content": _SUMMARIZE_SYSTEM},
                {"role": "user",   "content": user_content},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None
