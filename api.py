"""
SECP Adjudication RAG — FastAPI Backend
Wraps search_engine.py and summarize.py for the web UI.
"""

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pathlib import Path
from typing import Optional
import tempfile, json, sqlite3, sys, os

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).parent))
import config

# ---------------------------------------------------------------------------
# Safety / Refusal Layer
# ---------------------------------------------------------------------------

_SAFETY_SYSTEM = """\
You are a safety classifier for a SECP (Securities and Exchange Commission of Pakistan) \
adjudication orders research assistant.

The assistant is scoped ONLY to retrieving and presenting factual information from \
publicly available SECP adjudication orders. It does NOT provide legal advice, \
compliance recommendations, adjudication predictions, or access to non-public information.

Classify the user query into exactly one category:

  "safe"                     — Asking to retrieve/find/list/summarise information that
                               is present in public SECP adjudication orders
                               (penalties, violations, provisions, entities, dates, etc.)
  "legal_advice"             — Asking for legal advice, compliance guidance, how to avoid
                               penalties, or what action the company/person should take
  "adjudication_recommendation" — Asking the system to recommend, predict, or decide an
                               adjudication outcome or suggest what SECP should do
  "speculation"              — Asking to infer unstated reasons, guess intent, or speculate
                               on regulatory thinking not written in the orders
  "non_public"               — Asking for internal SECP documents, unreported cases,
                               confidential proceedings, or information not in public orders
  "external_comparison"      — Asking to compare SECP with other regulators, jurisdictions,
                               or make broad regulatory assessments beyond the documents

Return ONLY a JSON object:
{"category": "<one of the above>", "reason": "<very brief reason>"}\
"""

_REFUSAL_MESSAGES: dict[str, str] = {
    "legal_advice": (
        "This assistant provides information from SECP adjudication orders only — it does "
        "not offer legal advice or compliance guidance. For advice specific to your situation, "
        "please consult a qualified legal professional."
    ),
    "adjudication_recommendation": (
        "This assistant cannot recommend or predict adjudication outcomes. It retrieves and "
        "presents factual information from existing published SECP orders only."
    ),
    "speculation": (
        "This assistant only presents information explicitly stated in SECP adjudication "
        "orders. It cannot speculate on regulatory intent, infer unstated reasoning, or "
        "draw conclusions beyond what the orders contain."
    ),
    "non_public": (
        "The requested information is not available in publicly published SECP adjudication "
        "orders. This system only accesses documents in the public SECP Document Centre."
    ),
    "external_comparison": (
        "This assistant is scoped to SECP adjudication orders only and cannot make "
        "comparisons with other regulatory bodies or jurisdictions."
    ),
}

_openai_safety_client = None


def _get_openai():
    global _openai_safety_client
    if _openai_safety_client is None and config.OPENAI_API_KEY:
        from openai import OpenAI
        _openai_safety_client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _openai_safety_client


def check_query_safety(query: str) -> Optional[str]:
    """
    Returns a refusal message string if the query is out-of-scope, else None.
    Falls back to None (allow) if the OpenAI call fails.
    """
    client = _get_openai()
    if not client:
        return None
    try:
        resp = client.chat.completions.create(
            model=config.QUERY_PARSE_MODEL,
            messages=[
                {"role": "system", "content": _SAFETY_SYSTEM},
                {"role": "user",   "content": query},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)
        category = result.get("category", "safe")
        return _REFUSAL_MESSAGES.get(category)   # None for "safe"
    except Exception:
        return None   # On error, allow the query through

app = FastAPI(title="SECP Adjudication RAG", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Singleton search engine  (Qdrant holds a file lock — one instance only)
# ---------------------------------------------------------------------------
_engine = None

def get_engine():
    global _engine
    if _engine is None:
        from search_engine import SearchEngine
        _engine = SearchEngine()
    return _engine


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    use_llm: bool = True

class ChatSessionCreate(BaseModel):
    title: str
    messages: list[dict]

class ChatSessionUpdate(BaseModel):
    messages: list[dict]
    title: Optional[str] = None

class ConsolidatedRequest(BaseModel):
    doc_ids: list[str]
    scope: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/stats")
def stats():
    import mongo_store
    try:
        from qdrant_client import QdrantClient
        client = QdrantClient(path=str(config.QDRANT_PATH))
        info = client.get_collection(config.QDRANT_COLLECTION)
        chunk_count = info.points_count
    except Exception:
        chunk_count = 0

    doc_count = mongo_store.count_docs()
    categories = mongo_store.get_categories()

    avg_conf = 0.0
    pending = 0
    try:
        conn = sqlite3.connect(str(Path("data/knowledge.db")))
        row = conn.execute("SELECT AVG(confidence) FROM field_confidence").fetchone()
        avg_conf = round(row[0] or 0, 2)
        row2 = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE review_status='pending'"
        ).fetchone()
        pending = row2[0]
        conn.close()
    except Exception:
        pass

    return {
        "doc_count":      doc_count,
        "chunk_count":    chunk_count,
        "avg_confidence": avg_conf,
        "pending_review": pending,
        "categories":     categories,
    }


@app.get("/api/documents")
def documents():
    import mongo_store
    result = []
    for d in mongo_store.get_all_docs():
        try:
            meta = d.get("extracted_metadata", {})
            provisions = meta.get("legal_provisions", [])
            acts = list({pr.get("act", "") for pr in provisions if pr.get("act")})
            result.append({
                "filename":  d["filename"],
                "doc_id":    d.get("doc_id", ""),
                "entity":    ", ".join(meta.get("entity_names", [])) or d["filename"],
                "date":      meta.get("order_date", ""),
                "status":    d.get("status", ""),
                "category":  d.get("category", ""),
                "sections":  list({pr.get("section", "").split("(")[0]
                                   for pr in provisions if pr.get("section")}),
                "acts":      acts,
                "penalty":   meta.get("penalty_pkr"),
            })
        except Exception:
            pass
    return sorted(result, key=lambda x: (x["category"], x["filename"]))


@app.post("/api/search")
def search(req: SearchRequest):
    # Safety check first
    if req.use_llm:
        refusal_message = check_query_safety(req.query)
        if refusal_message:
            return {
                "refusal": True,
                "message": refusal_message,
                "results": [],
                "query_info": {
                    "original": req.query,
                    "semantic": req.query,
                    "intent":   "",
                    "filters_applied": {},
                    "count":    0,
                },
            }

    try:
        engine = get_engine()
        # Browse queries (entity_category filter) use scroll and return everything;
        # pass a high top_k so no results are arbitrarily capped.
        effective_top_k = max(req.top_k, 200)

        results, parsed = engine.search(
            req.query, top_k=effective_top_k, use_llm=req.use_llm
        )
        filters_applied = {
            k: v for k, v in parsed.items()
            if not k.startswith("_")
            and k not in ("semantic_query", "search_intent")
            and v
        }
        intent = parsed.get("search_intent", "browse")

        # Analytics for stats queries
        analytics_data = None
        if intent == "stats":
            try:
                from analytics import run_analytics
                analytics_data = run_analytics(parsed)
            except Exception:
                analytics_data = None

        # Narrative synthesis for summarize queries
        narrative_data = None
        if intent == "summarize" and results:
            try:
                from synthesis import synthesize_summary
                narrative_data = synthesize_summary(req.query, [r.to_dict() for r in results])
            except Exception:
                narrative_data = None

        return {
            "refusal": False,
            "results": [r.to_dict() for r in results],
            "analytics": analytics_data,
            "narrative": narrative_data,
            "query_info": {
                "original":        parsed.get("_original_query", req.query),
                "semantic":        parsed.get("semantic_query", req.query),
                "intent":          intent,
                "filters_applied": filters_applied,
                "entity_category": parsed.get("entity_category", ""),
                "sections":        parsed.get("sections", []),
                "acts":            parsed.get("acts", []),
                "count":           len(results),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def _parse_case_header(summary: dict) -> dict:
    """
    Normalise case_header into a structured dict the frontend can consume.
    build_summary() may return it as a formatted text block; we parse it here
    so the API always delivers a consistent object.
    """
    meta = summary.get("_meta", {})
    result = {
        "order_reference":  "",
        "respondent":       meta.get("respondent", ""),
        "order_date":       meta.get("order_date", ""),
        "laws_applied":     "",
        "issuing_authority":"",
    }

    hdr = summary.get("case_header", "")

    if isinstance(hdr, dict):
        result.update(hdr)
    elif isinstance(hdr, str):
        for line in hdr.split("\n"):
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if "reference" in key:
                result["order_reference"] = val
            elif "law" in key or "act" in key:
                result["laws_applied"] = val
            elif "authority" in key or "issuing" in key:
                result["issuing_authority"] = val
            elif "date" in key and "notice" not in key and "hearing" not in key:
                if not result["order_date"]:
                    result["order_date"] = val
            elif "respondent" in key:
                if not result["respondent"]:
                    result["respondent"] = val

    return result


@app.get("/api/chat/sessions")
def list_chat_sessions():
    import mongo_store
    return mongo_store.get_chat_sessions()


@app.post("/api/chat/sessions")
def create_chat_session(req: ChatSessionCreate):
    import mongo_store
    session_id = mongo_store.create_chat_session(req.title, req.messages)
    return {"session_id": session_id}


@app.get("/api/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    import mongo_store
    s = mongo_store.get_chat_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


@app.put("/api/chat/sessions/{session_id}")
def update_chat_session(session_id: str, req: ChatSessionUpdate):
    import mongo_store
    mongo_store.update_chat_session(session_id, req.messages, req.title)
    return {"ok": True}


@app.delete("/api/chat/sessions/{session_id}")
def delete_chat_session(session_id: str):
    import mongo_store
    mongo_store.delete_chat_session(session_id)
    return {"ok": True}


@app.get("/api/summaries")
def list_summaries():
    """Return summary cards for all documents that have a structured_summary."""
    import mongo_store
    result = []
    for d in mongo_store.get_all_docs():
        if not d.get("structured_summary"):
            continue
        meta = d.get("extracted_metadata", {})
        provisions = meta.get("legal_provisions") or []
        result.append({
            "doc_id":          d["doc_id"],
            "filename":        d["filename"],
            "entity":          ", ".join(meta.get("entity_names", [])) or d["filename"],
            "order_date":      meta.get("order_date", ""),
            "category":        d.get("category", ""),
            "sector":          meta.get("sector", "Other"),
            "action_types":    meta.get("action_types", []),
            "penalty_pkr":     meta.get("penalty_pkr"),
            "violations":      (meta.get("violations") or []),
            "entity_category": meta.get("entity_category", ""),
            "issuing_officer": meta.get("issuing_officer", ""),
            "order_reference": meta.get("order_reference", ""),
            "legal_provisions": provisions,
            "acts":            list({p.get("act","") for p in provisions if p.get("act")}),
            "sections":        list({p.get("section","") for p in provisions if p.get("section")}),
            "penalty_note":    meta.get("penalty_note", ""),
        })
    return sorted(result, key=lambda x: x.get("order_date") or "", reverse=True)


@app.get("/api/summaries/{doc_id}")
def get_summary(doc_id: str):
    """Return the full structured_summary for a document."""
    import mongo_store
    doc = mongo_store.get_doc(doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    s = doc.get("structured_summary")
    if not s:
        raise HTTPException(status_code=404, detail="Summary not yet generated")
    s = dict(s)
    s["case_header"] = _parse_case_header(s)
    return {"summary": s, "filename": doc["filename"]}


@app.post("/api/summaries/consolidated")
async def consolidated_summary(req: ConsolidatedRequest):
    """Generate a multi-case consolidated summary from selected documents."""
    import mongo_store
    from summarize import multi_case_summary

    if len(req.doc_ids) < 2:
        raise HTTPException(status_code=400, detail="Select at least 2 documents")

    docs = []
    for did in req.doc_ids:
        d = mongo_store.get_doc(did)
        if d and d.get("structured_summary"):
            docs.append(d)

    if len(docs) < 2:
        raise HTTPException(status_code=400, detail="Not enough documents with summaries found")

    try:
        summaries      = [d["structured_summary"] for d in docs]
        structured_list = [d.get("extracted_metadata", {}) for d in docs]
        result = multi_case_summary(summaries, structured_list, scope_desc=req.scope)
        return {
            "consolidated": result,
            "individual":   summaries,
            "count":        len(docs),
            "scope":        req.scope,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Consolidated summary failed: {exc}")


@app.post("/api/summarize")
async def summarize(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        from summarize import extract_text, extract_structured, build_summary
        full_text = extract_text(tmp_path)
        if not full_text or len(full_text.strip()) < 100:
            raise HTTPException(
                status_code=422,
                detail="Could not extract readable text from this PDF. "
                       "Ensure it is an SECP adjudication order.",
            )
        data = extract_structured(full_text)
        summary = build_summary(data, tmp_path)
        summary["case_header"]      = _parse_case_header(summary)
        summary["_source_filename"] = file.filename
        return {"summary": summary}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Summary failed: {exc}")
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Serve React build (production)
# ---------------------------------------------------------------------------
_dist = Path("frontend/dist")
if _dist.exists():
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    def serve_react(full_path: str):
        return FileResponse(str(_dist / "index.html"))


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=True)
