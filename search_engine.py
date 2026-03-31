"""
Phase 2 - SECP Adjudication Orders Search Engine
=================================================
Natural language + structured search over SECP adjudication orders stored in Qdrant.

Architecture
------------
  1. NLP Parser     (GPT-4o-mini)  -> structured filters + semantic query text
  2. Qdrant search  (server-side)  -> date range, penalty range, action type
  3. Post-filter    (Python-side)  -> sections, entity, officer, act, profession
     (Python post-filter is used for these fields because MatchText / MatchValue
      exact-match semantics miss prefix variants like "510" vs "510(2)" and
      substring matches like "Ittefaq" vs "M/s. Ittefaq Iron Industries Limited".
      The approach is: fetch wider from Qdrant, refine in Python.)
  4. Deduplication               -> best-scoring chunk per document
  5. Doc-map lookup              -> enrich with case_summary / key_facts from
                                    processed JSON (chunk 0 stores summary;
                                    non-zero chunks may not)
  6. Formatter                   -> rich terminal cards with all required fields

Usage
-----
  python search_engine.py                         # interactive mode (default)
  python search_engine.py -q "Section 510"        # single query, then exit
  python search_engine.py -q "..." -n 10          # show top 10 results
  python search_engine.py -q "..." --no-llm       # skip NLP parsing
  python search_engine.py -q "..." --json         # JSON output
  python search_engine.py --section 510 --min-penalty 500000   # structured flags

Required response fields (per spec)
-------------------------------------
  - Entity name / case title
  - Brief summary of key facts and outcome
  - Order date and reference number
  - Applicable legal provisions
  - Direct link to original order on secp.gov.pk (where available)
  - Issuing authority (officer / commissioner name)
  - Relevance ranking (0-100 %)
"""

from __future__ import annotations

import argparse
import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition, Filter, MatchAny, MatchValue, Range, ScoredPoint,
)

import config


# ==============================================================================
# Embedder
# ==============================================================================

_openai_client = None
_local_model   = None


def _openai():
    global _openai_client
    if _openai_client is None and config.OPENAI_API_KEY:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_client


def embed_query(text: str) -> list[float]:
    client = _openai()
    if client:
        resp = client.embeddings.create(model=config.EMBEDDING_MODEL, input=text.strip())
        return resp.data[0].embedding
    global _local_model
    if _local_model is None:
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer(config.LOCAL_EMBEDDING_MODEL)
    return _local_model.encode(text, normalize_embeddings=True).tolist()


# ==============================================================================
# NLP Query Parser
# ==============================================================================

_PARSE_SYSTEM = """\
You are a query parser for SECP (Securities and Exchange Commission of Pakistan)
adjudication orders. Extract structured search parameters from the user query.
Return ONLY a valid JSON object with these exact fields:

  "sections"         : list of bare legal section numbers (e.g. ["510","134","183"])
  "entity_names"     : list of specific company/entity names (partial OK, e.g. ["Ittefaq"]).
                       Do NOT put category words like "listed companies", "brokers",
                       "NBFC" here — use entity_category for those.
  "entity_category"  : one of "Listed Company" | "Unlisted Company" | "Broker" |
                       "Asset Management Company" | "NBFC" | "Insurance Company" | null.
                       Set this when the query refers to a type/category of entity,
                       e.g. "listed companies" → "Listed Company",
                            "unlisted company orders" → "Unlisted Company",
                            "broker cases" → "Broker".
  "individual_names" : list of individual person names (directors, CAs, auditors)
  "profession"       : professional category if mentioned — one of:
                       "chartered accountant", "auditor", "director", "CFO",
                       "company secretary" — or null
  "date_from"        : "YYYY-MM-DD" or null  (interpret "2025", "last year", etc.)
  "date_to"          : "YYYY-MM-DD" or null
  "penalty_min"      : minimum penalty in PKR as number or null
                       ("1 million" = 1000000, "PKR 500,000" = 500000)
  "penalty_max"      : maximum penalty in PKR as number or null
  "issuing_officer"  : officer name if mentioned, else null
  "action_types"     : subset of ["Penalty","Warning","Settlement",
                       "Licensing Action","Compliance Direction"] or []
  "order_reference"  : specific order reference string or null
  "acts"             : list of act names, e.g. ["Companies Act, 2017",
                       "Companies Ordinance 1984"] or []
  "semantic_query"   : concise rephrased query capturing intent
                       (remove filter info already captured above;
                        keep legal / conceptual meaning)
  "search_intent"    : one of:
                       "browse"    — user wants a list/all orders matching criteria
                                     (show, list, find all, how many, retrieve all)
                       "lookup"    — user wants a specific order by entity/date/reference
                       "summarize" — user wants a narrative summary of one or more orders
                       "stats"     — user wants counts, statistics, frequency analysis

Today is 2026-03-26. Return ONLY JSON, no explanation.\
"""


def parse_query(query: str) -> dict:
    """Use GPT-4o-mini to extract structured filters + semantic search text."""
    client = _openai()
    if not client:
        return {"semantic_query": query, "_original_query": query}
    try:
        resp = client.chat.completions.create(
            model=config.QUERY_PARSE_MODEL,
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM},
                {"role": "user",   "content": query},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(resp.choices[0].message.content)
        parsed["_original_query"] = query
        return parsed
    except Exception as exc:
        return {"semantic_query": query, "_original_query": query,
                "_parse_error": str(exc)}


# ==============================================================================
# Qdrant Filter Builder  (server-side — only reliable exact/range fields)
# ==============================================================================

def build_qdrant_filter(parsed: dict) -> Optional[Filter]:
    """
    Build a Qdrant server-side filter.
    Only uses fields with reliable numeric/range semantics:
      - order_date_iso  (string range  YYYY-MM-DD sorts lexicographically)
      - penalty_pkr     (float range)
      - action_types    (MatchAny on list field)

    Entity, section, officer, act, profession are handled in Python post-filter
    to support substring / prefix matching that MatchValue cannot do reliably.
    """
    must = []

    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")
    if date_from:
        must.append(FieldCondition(key="order_date_iso", range=Range(gte=date_from)))
    if date_to:
        must.append(FieldCondition(key="order_date_iso", range=Range(lte=date_to)))

    p_min = parsed.get("penalty_min")
    p_max = parsed.get("penalty_max")
    if p_min is not None or p_max is not None:
        # Always exclude the -1.0 sentinel that means "penalty not specified"
        must.append(FieldCondition(key="penalty_pkr", range=Range(gt=0.0)))
    if p_min is not None:
        must.append(FieldCondition(key="penalty_pkr", range=Range(gte=float(p_min))))
    if p_max is not None:
        must.append(FieldCondition(key="penalty_pkr", range=Range(lte=float(p_max))))

    action_types = [a for a in (parsed.get("action_types") or []) if a]
    if action_types:
        must.append(FieldCondition(key="action_types", match=MatchAny(any=action_types)))

    entity_category = (parsed.get("entity_category") or "").strip()
    if entity_category:
        must.append(FieldCondition(key="entity_category",
                                   match=MatchValue(value=entity_category)))

    return Filter(must=must) if must else None


# ==============================================================================
# Python Post-Filter  (flexible substring / prefix matching)
# ==============================================================================

def post_filter(hits: list[ScoredPoint], parsed: dict,
                doc_map: Optional[dict] = None) -> list[ScoredPoint]:
    """
    Apply flexible matching on fields that need substring or prefix semantics:
      - sections    : "510" matches "510", "510(2)", "510(1)(a)" (prefix match)
      - entity_names: substring match, case-insensitive
      - individual_names / profession: searched in respondents + violations + summary
      - issuing_officer: substring match
      - acts        : substring match
    """
    f_sections    = [str(s).strip() for s in (parsed.get("sections") or [])]
    f_entities    = [e.lower().strip() for e in (parsed.get("entity_names") or [])]
    f_individuals = [n.lower().strip() for n in (parsed.get("individual_names") or [])]
    f_officer     = (parsed.get("issuing_officer") or "").lower().strip()
    f_profession  = (parsed.get("profession") or "").lower().strip()
    f_acts        = [a.lower().strip() for a in (parsed.get("acts") or [])]

    # No post-filter criteria -> return all
    if not any([f_sections, f_entities, f_individuals, f_officer, f_profession, f_acts]):
        return hits

    out = []
    for hit in hits:
        pl = hit.payload

        hit_sections    = [str(s) for s in (pl.get("sections") or [])]
        hit_entities    = " ".join(pl.get("entity_names") or []).lower()
        hit_individuals = " ".join(pl.get("individual_respondents") or []).lower()
        hit_officer     = (pl.get("issuing_officer") or "").lower()
        hit_acts        = " ".join(pl.get("acts") or []).lower()
        # Enrich from doc_map when chunk is not chunk-0 (case_summary/violations may be empty)
        doc_id  = pl.get("doc_id", "")
        doc_em  = ((doc_map or {}).get(doc_id) or {}).get("extracted_metadata", {})
        hit_violations  = " ".join(pl.get("violations") or doc_em.get("violations") or []).lower()
        hit_summary     = (pl.get("case_summary") or doc_em.get("case_summary") or "").lower()
        # Combined text for profession / individual matching
        hit_text        = " ".join([hit_individuals, hit_violations, hit_summary])

        # Section: filter value is a prefix of a stored section OR vice versa.
        # "510" matches "510", "510(2)"; "510(2)" also matches "510" stored as base.
        if f_sections:
            matched = any(
                hs.startswith(fs) or fs.startswith(hs)
                for fs in f_sections
                for hs in hit_sections
            )
            if not matched:
                continue

        if f_entities and not any(fe in hit_entities for fe in f_entities):
            continue

        if f_individuals and not any(fi in hit_text for fi in f_individuals):
            continue

        if f_officer and f_officer not in hit_officer:
            continue

        if f_acts and not any(fa in hit_acts for fa in f_acts):
            continue

        if f_profession and f_profession not in hit_text:
            continue

        out.append(hit)
    return out


# ==============================================================================
# Result dataclass
# ==============================================================================

@dataclass
class SearchResult:
    rank:                   int
    relevance_pct:          int           # 0-100
    entity_names:           list[str]
    individual_respondents: list[str]
    entity_category:        str
    order_reference:        str
    order_date:             str
    case_summary:           str
    key_facts:              list[str]
    legal_provisions:       list[dict]
    sections:               list[str]
    acts:                   list[str]
    violations:             list[str]
    penalty_display:        str
    issuing_officer:        str
    action_types:           list[str]
    source_url:             str
    filename:               str
    doc_id:                 str
    raw_score:              float

    @property
    def case_title(self) -> str:
        """Human-readable title: 'Order Dated <date> against <entity>'."""
        entity = ", ".join(
            e.lstrip("M/s. ").strip() for e in self.entity_names
        ) if self.entity_names else self.filename
        parts = ["Adjudication Order"]
        if self.order_date:
            try:
                from datetime import datetime
                d = datetime.strptime(self.order_date, "%Y-%m-%d")
                parts[0] = f"Order Dated {d.strftime('%-d %B %Y')}"
            except Exception:
                parts[0] = f"Order Dated {self.order_date}"
        if entity:
            parts.append(f"against {entity}")
        if self.legal_provisions:
            # Group sections by act
            act_map: dict[str, list[str]] = {}
            for p in self.legal_provisions:
                a = p.get("act", "")
                s = p.get("section", "")
                if s:
                    act_map.setdefault(a, []).append(s)
            prov_parts = []
            for act, secs in act_map.items():
                sec_str = " read with ".join(f"Section {s}" for s in secs)
                prov_parts.append(f"{sec_str} of the {act}" if act else sec_str)
            if prov_parts:
                parts.append("under " + "; ".join(prov_parts))
        return " ".join(parts)

    def to_dict(self) -> dict:
        return {
            "rank":                   self.rank,
            "relevance_pct":          self.relevance_pct,
            "case_title":             self.case_title,
            "entity_names":           self.entity_names,
            "individual_respondents": self.individual_respondents,
            "entity_category":        self.entity_category,
            "order_reference":        self.order_reference,
            "order_date":             self.order_date,
            "case_summary":           self.case_summary,
            "key_facts":              self.key_facts,
            "legal_provisions":       self.legal_provisions,
            "sections":               self.sections,
            "acts":                   self.acts,
            "violations":             self.violations,
            "penalty_display":        self.penalty_display,
            "issuing_officer":        self.issuing_officer,
            "action_types":           self.action_types,
            "source_url":             self.source_url,
            "filename":               self.filename,
        }


# ==============================================================================
# Search Engine
# ==============================================================================

class SearchEngine:

    def __init__(self):
        self.client  = QdrantClient(path=str(config.QDRANT_PATH))
        self.doc_map = self._load_doc_map()
        self._total_docs = self.client.count(
            collection_name=config.QDRANT_COLLECTION
        ).count

    # ------------------------------------------------------------------
    def _load_doc_map(self) -> dict[str, dict]:
        """Build doc_id -> processed-doc mapping for summary / key_facts lookup."""
        doc_map = {}
        try:
            import mongo_store
            for d in mongo_store.get_all_docs():
                doc_map[d["doc_id"]] = d
        except Exception:
            # Fallback to JSON files if MongoDB unavailable
            for p in config.PROCESSED_DIR.glob("*.json"):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    doc_map[d["doc_id"]] = d
                except Exception:
                    pass
        return doc_map

    # ------------------------------------------------------------------
    def _is_browse_query(self, parsed: dict) -> bool:
        """
        Use scroll mode (100 % recall) when the query is a list/browse request
        with only structural filters — no specific entity/individual target that
        needs semantic matching.

        Scroll guarantees every matching document is returned.
        Semantic mode is reserved for targeted lookups and summaries where
        cosine similarity meaningfully ranks results.
        """
        has_structural_filter = any([
            parsed.get("entity_category"),
            parsed.get("acts"),
            parsed.get("sections"),
            parsed.get("date_from"),
            parsed.get("date_to"),
            # penalty_min/max may be 0 so check explicitly for non-None
            parsed.get("penalty_min") is not None,
            parsed.get("penalty_max") is not None,
            parsed.get("action_types"),
        ])

        has_specific_target = any([
            parsed.get("entity_names"),
            parsed.get("individual_names"),
            parsed.get("issuing_officer"),
            parsed.get("order_reference"),
        ])

        intent_is_browse = parsed.get("search_intent") in ("browse", "stats")

        return (has_structural_filter and not has_specific_target) or intent_is_browse

    def _scroll_all(self, qdrant_filter: Optional[Filter]) -> list[ScoredPoint]:
        """
        Retrieve every chunk 0 matching the filter via Qdrant scroll.
        Returns fake ScoredPoints (score=1.0) sorted by order_date descending.
        """
        from qdrant_client.models import PointStruct

        # Only retrieve chunk_index=0 (one representative per document)
        chunk0_filter = Filter(must=[
            FieldCondition(key="chunk_index", match=MatchValue(value=0)),
            *(qdrant_filter.must if qdrant_filter else []),
        ])

        results = []
        offset = None
        while True:
            batch, next_offset = self.client.scroll(
                collection_name=config.QDRANT_COLLECTION,
                scroll_filter=chunk0_filter,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            results.extend(batch)
            if next_offset is None:
                break
            offset = next_offset

        # Wrap as ScoredPoint-like objects with a synthetic score
        class _FakeScored:
            def __init__(self, point):
                self.id      = point.id
                self.score   = 1.0
                self.payload = point.payload

        scored = [_FakeScored(p) for p in results]
        # Sort by order_date descending (most recent first)
        scored.sort(key=lambda h: h.payload.get("order_date_iso") or "", reverse=True)
        return scored

    # ------------------------------------------------------------------
    def search(
        self,
        query:       str,
        top_k:       int  = config.DEFAULT_TOP_K,
        use_llm:     bool = True,
    ) -> tuple[list[SearchResult], dict]:
        """
        Main entry point. Returns (results, parsed_query).

        Flow (semantic):
          parse -> embed -> Qdrant vector search -> dedup -> post-filter -> rank

        Flow (browse / category filter):
          parse -> Qdrant scroll (exact filter, chunk_index=0) -> sort by date
        """
        parsed     = parse_query(query) if use_llm else {"semantic_query": query,
                                                          "_original_query": query}
        semantic_q = parsed.get("semantic_query") or query

        qdrant_filter = build_qdrant_filter(parsed)

        if self._is_browse_query(parsed):
            # ── Browse mode: scroll entire index, filter in Python ────────────
            # Qdrant scroll handles date/penalty/action_type/entity_category
            # filters (set in qdrant_filter).  Acts and sections are then
            # applied in Python post_filter for substring/prefix matching.
            hits = self._scroll_all(qdrant_filter)
            hits = post_filter(hits, parsed, doc_map=self.doc_map)
            # Already sorted by date descending inside _scroll_all;
            # post_filter preserves order.
        else:
            # ── Semantic mode: vector search with score threshold ─────────────
            # 1. Embed
            vector = embed_query(semantic_q)

            # 2. Qdrant vector search (server-side date/penalty/action/category filter)
            response = self.client.query_points(
                collection_name=config.QDRANT_COLLECTION,
                query=vector,
                query_filter=qdrant_filter,
                limit=80,        # wide fetch for dedup + post-filter
                with_payload=True,
                with_vectors=False,
                score_threshold=0.25,
            )
            raw_hits = response.points

            # 3. Deduplicate — best chunk per document
            best: dict[str, ScoredPoint] = {}
            for hit in raw_hits:
                doc_id = hit.payload.get("doc_id", "")
                if doc_id not in best or hit.score > best[doc_id].score:
                    best[doc_id] = hit

            hits = list(best.values())

            # 4. Python post-filter (sections, entity, officer, act, profession)
            hits = post_filter(hits, parsed, doc_map=self.doc_map)

            # 5. Sort: by date for browse/stats intent, by relevance otherwise
            if parsed.get("search_intent") in ("browse", "stats"):
                hits.sort(
                    key=lambda h: h.payload.get("order_date_iso") or "",
                    reverse=True,
                )
            else:
                hits.sort(key=lambda h: h.score, reverse=True)
            hits = hits[:top_k]

        # 6. Build SearchResult objects (enrich from doc_map)
        results = [self._build_result(hit, rank) for rank, hit in enumerate(hits, 1)]
        return results, parsed

    # ------------------------------------------------------------------
    def structured_search(
        self,
        query:           Optional[str]       = None,
        sections:        Optional[list[str]] = None,
        entity_name:     Optional[str]       = None,
        individual_name: Optional[str]       = None,
        profession:      Optional[str]       = None,
        act:             Optional[str]       = None,
        date_from:       Optional[str]       = None,
        date_to:         Optional[str]       = None,
        min_penalty:     Optional[float]     = None,
        max_penalty:     Optional[float]     = None,
        action_types:    Optional[list[str]] = None,
        officer:         Optional[str]       = None,
        order_reference: Optional[str]       = None,
        top_k:           int                 = config.DEFAULT_TOP_K,
    ) -> tuple[list[SearchResult], dict]:
        """Structured search with explicit parameters (bypasses NLP parsing)."""
        parsed = {
            "_original_query": query or "",
            "semantic_query":  query or "",
            "sections":        sections or [],
            "entity_names":    [entity_name] if entity_name else [],
            "entity_category": "",
            "individual_names":[individual_name] if individual_name else [],
            "profession":      profession or "",
            "acts":            [act] if act else [],
            "date_from":       date_from,
            "date_to":         date_to,
            "penalty_min":     min_penalty,
            "penalty_max":     max_penalty,
            "action_types":    action_types or [],
            "issuing_officer": officer or "",
            "order_reference": order_reference or "",
        }
        return self.search(query or "adjudication order", top_k=top_k, use_llm=False)

    # ------------------------------------------------------------------
    def _build_result(self, hit: ScoredPoint, rank: int) -> SearchResult:
        pl     = hit.payload
        doc_id = pl.get("doc_id", "")
        doc    = self.doc_map.get(doc_id, {})
        em     = doc.get("extracted_metadata", {}) if doc else {}

        # Case summary — chunk 0 stores it; non-zero chunks have "" so fall back
        case_summary = pl.get("case_summary") or em.get("case_summary") or ""
        key_facts    = em.get("key_facts", [])

        # Penalty
        raw_penalty = pl.get("penalty_pkr", -1.0)
        if raw_penalty and raw_penalty > 0:
            penalty_display = f"PKR {int(raw_penalty):,}"
        else:
            note = pl.get("penalty_note") or em.get("penalty_note") or ""
            penalty_display = note if note else "Not specified"

        # Source URL — resolve to correct category page if not explicitly stored
        _ACT_URLS = {
            "Companies Act, 2017":
                "https://www.secp.gov.pk/enforcement/orders/orders-issued-under-companies-act-2017/",
            "Companies Rules, 1996":
                "https://www.secp.gov.pk/enforcement/orders/companies-rules-1996/",
            "Companies (General Provisions & Forms) Rules, 1985":
                "https://www.secp.gov.pk/enforcement/orders/companies-general-provisions-forms-rules-1985/",
            "Companies (Amendments) Ordinance, 2002":
                "https://www.secp.gov.pk/enforcement/orders/companies-amendments-ordinance-2002/",
            "Listed Companies Order, 2002":
                "https://www.secp.gov.pk/enforcement/orders/listed-companies-order-2002/",
        }
        source_url = (pl.get("source_url") or doc.get("source_url") or "").strip()
        if not source_url:
            _acts = pl.get("acts") or []
            _provs = pl.get("legal_provisions") or em.get("legal_provisions") or []
            for _act in _acts:
                if _act in _ACT_URLS:
                    source_url = _ACT_URLS[_act]
                    break
            if not source_url:
                for _p in _provs:
                    if _p.get("act") in _ACT_URLS:
                        source_url = _ACT_URLS[_p["act"]]
                        break
            if not source_url:
                source_url = "https://www.secp.gov.pk/enforcement/orders/"

        # Relevance: cosine scores for OpenAI embeddings sit roughly in [0.3, 0.95]
        # Normalise to [0, 100] using [0.25, 1.0] as the expected range.
        # Browse-mode hits have synthetic score=1.0 — show as 0 (not displayed).
        raw = hit.score
        if raw == 1.0 and pl.get("chunk_index") == 0:
            # Browse mode: no meaningful relevance score
            pct = 0
        else:
            norm = (raw - 0.25) / (1.0 - 0.25)
            pct  = min(99, max(1, round(norm * 100)))

        return SearchResult(
            rank=rank,
            relevance_pct=pct,
            entity_names=pl.get("entity_names") or [],
            individual_respondents=pl.get("individual_respondents") or [],
            entity_category=pl.get("entity_category") or em.get("entity_category") or "",
            order_reference=pl.get("order_reference") or "",
            order_date=pl.get("order_date_iso") or "",
            case_summary=case_summary,
            key_facts=key_facts,
            legal_provisions=pl.get("legal_provisions") or em.get("legal_provisions") or [],
            sections=pl.get("sections") or [],
            acts=pl.get("acts") or [],
            violations=pl.get("violations") or em.get("violations") or [],
            penalty_display=penalty_display,
            issuing_officer=pl.get("issuing_officer") or "",
            action_types=pl.get("action_types") or [],
            source_url=source_url,
            filename=pl.get("filename") or "",
            doc_id=doc_id,
            raw_score=raw,
        )


# ==============================================================================
# Formatter
# ==============================================================================

W = 72   # card width


def _rule(char: str = "=") -> str:
    return char * W


def _wrap(text: str, label_len: int) -> str:
    """Wrap text with hanging indent aligned to label."""
    indent   = " " * label_len
    avail    = W - label_len
    lines    = textwrap.wrap(text, width=avail)
    if not lines:
        return ""
    return ("\n" + indent).join(lines)


def _fmt_date(iso: str) -> str:
    if not iso:
        return "N/A"
    try:
        from datetime import datetime
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return iso


def _fmt_provisions(provisions: list[dict]) -> str:
    parts = []
    for p in provisions:
        sec = p.get("section", "")
        act = p.get("act", "")
        clause = p.get("clause") or ""
        if sec and act:
            parts.append(f"Sec {sec} - {act}" + (f" [{clause}]" if clause else ""))
        elif sec:
            parts.append(f"Sec {sec}")
        elif act:
            parts.append(act)
    return " | ".join(parts) if parts else ""


def format_result_card(result: SearchResult) -> str:
    lines = []
    entity = ", ".join(result.entity_names) if result.entity_names else "Unknown Entity"

    # ── Header ────────────────────────────────────────────────────────────────
    pct_tag   = f"[{result.relevance_pct}% match]"
    title_str = f" #{result.rank}  {entity}"
    pad       = W - len(pct_tag) - 1
    lines.append(_rule("="))
    lines.append(f"{title_str:<{pad}}{pct_tag}")
    lines.append(_rule("-"))

    # ── Core fields ───────────────────────────────────────────────────────────
    def row(label: str, value: str):
        if not value or value.strip() in ("N/A", "Not specified", ""):
            return
        prefix = f"  {label:<12}: "
        lines.append(prefix + _wrap(value, len(prefix)))

    row("Reference",  result.order_reference)
    row("Date",       _fmt_date(result.order_date))
    row("Action",     ", ".join(result.action_types))
    row("Penalty",    result.penalty_display)

    # Provisions
    prov_str = _fmt_provisions(result.legal_provisions)
    if not prov_str and result.sections:
        prov_str = " | ".join(f"Sec {s}" for s in result.sections)
    row("Provisions",  prov_str)

    row("Officer",    result.issuing_officer)

    # Source / link — resolve to correct category URL if no explicit URL stored
    _ACT_URLS = {
        "Companies Act, 2017":
            "https://www.secp.gov.pk/enforcement/orders/orders-issued-under-companies-act-2017/",
        "Companies Rules, 1996":
            "https://www.secp.gov.pk/enforcement/orders/companies-rules-1996/",
        "Companies (General Provisions & Forms) Rules, 1985":
            "https://www.secp.gov.pk/enforcement/orders/companies-general-provisions-forms-rules-1985/",
        "Companies (Amendments) Ordinance, 2002":
            "https://www.secp.gov.pk/enforcement/orders/companies-amendments-ordinance-2002/",
        "Listed Companies Order, 2002":
            "https://www.secp.gov.pk/enforcement/orders/listed-companies-order-2002/",
    }
    src = result.source_url
    if not src:
        for act in result.acts:
            if act in _ACT_URLS:
                src = _ACT_URLS[act]
                break
        if not src:
            for p in result.legal_provisions:
                if p.get("act") in _ACT_URLS:
                    src = _ACT_URLS[p["act"]]
                    break
    row("Source", src or "https://www.secp.gov.pk/enforcement/orders/")

    # ── Violations ────────────────────────────────────────────────────────────
    if result.violations:
        lines.append(_rule("-"))
        lines.append("  Violations:")
        for v in result.violations[:5]:
            for sub in textwrap.wrap(v, width=W - 6):
                lines.append(f"    - {sub}")
        if len(result.violations) > 5:
            lines.append(f"    ... and {len(result.violations) - 5} more")

    # ── Summary ───────────────────────────────────────────────────────────────
    if result.case_summary:
        lines.append(_rule("-"))
        lines.append("  Summary:")
        for line in textwrap.wrap(result.case_summary, width=W - 4):
            lines.append(f"    {line}")

    # ── Key Facts ─────────────────────────────────────────────────────────────
    if result.key_facts:
        lines.append("  Key Facts:")
        for kf in result.key_facts[:3]:
            for sub in textwrap.wrap(kf, width=W - 6):
                lines.append(f"    - {sub}")

    # ── Respondents ───────────────────────────────────────────────────────────
    if result.individual_respondents:
        lines.append(_rule("-"))
        lines.append("  Respondents:")
        for r in result.individual_respondents[:6]:
            lines.append(f"    - {r}")
        if len(result.individual_respondents) > 6:
            lines.append(f"    ... and {len(result.individual_respondents) - 6} more")

    lines.append(_rule("="))
    return "\n".join(lines)


def format_header(query: str, results: list[SearchResult], parsed: dict) -> str:
    lines = ["\n" + _rule(), "  SECP Adjudication Orders  |  Search Results", _rule()]
    lines.append(f"  Query   : {query}")

    # Applied filters summary
    fparts = []
    if parsed.get("sections"):
        fparts.append("Sections: " + ", ".join(str(s) for s in parsed["sections"]))
    if parsed.get("entity_names"):
        fparts.append("Entity: " + ", ".join(parsed["entity_names"]))
    if parsed.get("individual_names"):
        fparts.append("Individual: " + ", ".join(parsed["individual_names"]))
    if parsed.get("profession"):
        fparts.append("Profession: " + parsed["profession"])
    if parsed.get("date_from") or parsed.get("date_to"):
        fparts.append(f"Date: {parsed.get('date_from','*')} to {parsed.get('date_to','*')}")
    if parsed.get("penalty_min") is not None:
        fparts.append(f"Min Penalty: PKR {int(parsed['penalty_min']):,}")
    if parsed.get("penalty_max") is not None:
        fparts.append(f"Max Penalty: PKR {int(parsed['penalty_max']):,}")
    if parsed.get("action_types"):
        fparts.append("Action: " + ", ".join(parsed["action_types"]))
    if parsed.get("acts"):
        fparts.append("Acts: " + ", ".join(parsed["acts"]))
    if parsed.get("issuing_officer"):
        fparts.append("Officer: " + parsed["issuing_officer"])

    if fparts:
        # Wrap if multiple filters
        filter_str = " | ".join(fparts)
        prefix = "  Filters : "
        lines.append(prefix + _wrap(filter_str, len(prefix)))

    if not results:
        lines.append("\n  No matching orders found.")
        lines.append(_rule())
    else:
        lines.append(f"  Found   : {len(results)} order(s)")
        lines.append(_rule())

    return "\n".join(lines)


def render_results(
    query: str,
    results: list[SearchResult],
    parsed: dict,
    as_json: bool = False,
) -> str:
    if as_json:
        return json.dumps([r.to_dict() for r in results], indent=2, ensure_ascii=False)

    parts = [format_header(query, results, parsed)]
    for r in results:
        parts.append("\n" + format_result_card(r))
    return "\n".join(parts)


def _safe_print(text: str) -> None:
    """Print with ASCII fallback for Windows terminals that lack UTF-8 support."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


# ==============================================================================
# Interactive CLI
# ==============================================================================

_HELP = """
  Query examples
  --------------
  What penalties have been imposed under Section 510?
  Show adjudication cases against chartered accountants
  Find orders involving Ittefaq Iron Industries
  Orders related to audit report violations
  Cases with penalties exceeding PKR 1 million
  Show cases under Companies Ordinance 1984
  Orders about disclosure requirement failures
  Find cases involving going concern issues
  What actions were taken against auditors?
  Orders issued in 2025 against listed companies
  Cases by officer Sohail Qadri with penalty above 100000

  Commands: 'help', 'exit'
"""


def interactive_cli(engine: SearchEngine, top_k: int, use_llm: bool) -> None:
    _safe_print("\n" + _rule())
    _safe_print("  SECP Adjudication Orders  |  Search Engine")
    _safe_print(f"  {len(engine.doc_map)} documents loaded  |  "
                f"{engine._total_docs} indexed chunks  |  "
                f"{'LLM parsing ON' if use_llm else 'LLM parsing OFF'}")
    _safe_print("  Type your query. Commands: 'help', 'exit'")
    _safe_print(_rule())

    while True:
        try:
            query = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not query:
            continue
        if query.lower() in ("exit", "quit", "q"):
            print("Goodbye.")
            break
        if query.lower() == "help":
            _safe_print(_HELP)
            continue

        try:
            results, parsed = engine.search(query, top_k=top_k, use_llm=use_llm)
        except Exception as exc:
            print(f"\n  [ERROR] {exc}")
            continue

        _safe_print(render_results(query, results, parsed))


# ==============================================================================
# CLI entry point
# ==============================================================================

def main() -> None:
    ap = argparse.ArgumentParser(
        description="SECP Adjudication Orders Search Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-q", "--query",       type=str,   default=None)
    ap.add_argument("-n", "--top",         type=int,   default=config.DEFAULT_TOP_K,
                    help="Number of results (default %(default)s)")
    ap.add_argument("--no-llm",            action="store_true",
                    help="Disable NLP query parsing (pure vector search)")
    ap.add_argument("--json",              action="store_true",
                    help="Output results as JSON (single query mode only)")
    # Structured flags (bypass NLP)
    ap.add_argument("--section",           action="append", dest="sections",
                    help="Section number (repeatable)")
    ap.add_argument("--entity",            type=str)
    ap.add_argument("--individual",        type=str)
    ap.add_argument("--profession",        type=str)
    ap.add_argument("--act",               type=str)
    ap.add_argument("--date-from",         type=str)
    ap.add_argument("--date-to",           type=str)
    ap.add_argument("--min-penalty",       type=float)
    ap.add_argument("--max-penalty",       type=float)
    ap.add_argument("--action-type",       action="append", dest="action_types")
    ap.add_argument("--officer",           type=str)
    ap.add_argument("--order-ref",         type=str)
    args = ap.parse_args()

    print("Loading...", end=" ", flush=True)
    engine = SearchEngine()
    print(f"OK  ({len(engine.doc_map)} docs)")

    use_llm = not args.no_llm

    has_structured = any([
        args.sections, args.entity, args.individual, args.profession,
        args.act, args.date_from, args.date_to,
        args.min_penalty, args.max_penalty, args.action_types,
        args.officer, args.order_ref,
    ])

    if has_structured:
        results, parsed = engine.structured_search(
            query=args.query, sections=args.sections, entity_name=args.entity,
            individual_name=args.individual, profession=args.profession,
            act=args.act, date_from=args.date_from, date_to=args.date_to,
            min_penalty=args.min_penalty, max_penalty=args.max_penalty,
            action_types=args.action_types, officer=args.officer,
            order_reference=args.order_ref, top_k=args.top,
        )
        _safe_print(render_results(args.query or "", results, parsed, as_json=args.json))

    elif args.query:
        results, parsed = engine.search(args.query, top_k=args.top, use_llm=use_llm)
        _safe_print(render_results(args.query, results, parsed, as_json=args.json))

    else:
        interactive_cli(engine, top_k=args.top, use_llm=use_llm)


if __name__ == "__main__":
    main()
