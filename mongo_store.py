"""
MongoDB storage layer for the SECP RAG pipeline.

Replaces processed_docs/*.json flat-file store.

Collections
───────────
  documents     one document per processed PDF
  audit_log     one entry per ingestion event (append-only)
  chat_sessions one document per chat conversation
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import uuid

from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.collection import Collection

import config

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

_client: Optional[MongoClient] = None


def _get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(config.MONGO_URI, serverSelectionTimeoutMS=5000)
    return _client


def get_db():
    return _get_client()[config.MONGO_DB]


# ---------------------------------------------------------------------------
# Collections + indexes (created once, idempotent)
# ---------------------------------------------------------------------------

def _docs_col() -> Collection:
    col = get_db()["documents"]
    col.create_index("doc_id",   unique=True, background=True)
    col.create_index("filename",              background=True)
    col.create_index("category",              background=True)
    col.create_index("status",                background=True)
    col.create_index(
        [("extracted_metadata.order_date", ASCENDING)], background=True
    )
    return col


def _audit_col() -> Collection:
    col = get_db()["audit_log"]
    col.create_index("filename",  background=True)
    col.create_index("doc_id",    background=True)
    col.create_index("timestamp", background=True)
    return col


# ---------------------------------------------------------------------------
# Documents CRUD
# ---------------------------------------------------------------------------

def upsert_doc(doc: dict) -> str:
    """
    Insert or fully replace a document identified by doc_id.
    Returns doc_id.
    """
    _docs_col().replace_one(
        {"doc_id": doc["doc_id"]},
        doc,
        upsert=True,
    )
    return doc["doc_id"]


def update_doc_field(doc_id: str, field: str, value) -> None:
    """Update a single top-level field of a document."""
    _docs_col().update_one(
        {"doc_id": doc_id},
        {"$set": {field: value}},
    )


def get_doc(doc_id: str) -> Optional[dict]:
    return _docs_col().find_one({"doc_id": doc_id}, {"_id": 0})


def get_doc_by_filename(filename: str) -> Optional[dict]:
    return _docs_col().find_one({"filename": filename}, {"_id": 0})


def get_all_docs(
    query: dict = None,
    projection: dict = None,
) -> list[dict]:
    proj = {**(projection or {}), "_id": 0}
    return list(_docs_col().find(query or {}, proj))


def doc_exists_by_checksum(checksum: str) -> bool:
    """
    Returns True if a non-failed document with this checksum already exists.
    Used by IncrementalTracker to skip unchanged files.
    """
    return (
        _docs_col().count_documents(
            {"doc_id": checksum, "status": {"$ne": "failed"}},
            limit=1,
        )
        > 0
    )


def get_partial_docs() -> list[dict]:
    """Return docs with status partial or scanned (for --reprocess-partial)."""
    return get_all_docs(
        query={"status": {"$in": ["partial", "scanned"]}},
        projection={"doc_id": 1, "filename": 1, "file_path": 1,
                    "status": 1, "category": 1},
    )


def count_docs() -> int:
    return _docs_col().count_documents({})


def get_categories() -> list[str]:
    return _docs_col().distinct("category")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def append_audit(entry: dict) -> None:
    entry.setdefault(
        "timestamp",
        datetime.now(timezone.utc).isoformat(),
    )
    _audit_col().insert_one(entry)


def get_audit_entries(query: dict = None) -> list[dict]:
    return list(
        _audit_col().find(query or {}, {"_id": 0}).sort("timestamp", ASCENDING)
    )


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

def _chat_col() -> Collection:
    col = get_db()["chat_sessions"]
    col.create_index("session_id", unique=True, background=True)
    col.create_index("updated_at",              background=True)
    return col


def create_chat_session(title: str, messages: list) -> str:
    """Create a new chat session. Returns session_id."""
    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    _chat_col().insert_one({
        "session_id":    session_id,
        "title":         title[:120],
        "messages":      messages,
        "message_count": len(messages),
        "created_at":    now,
        "updated_at":    now,
    })
    return session_id


def update_chat_session(session_id: str, messages: list, title: str | None = None) -> None:
    """Replace messages in an existing session."""
    update: dict = {
        "messages":      messages,
        "message_count": len(messages),
        "updated_at":    datetime.now(timezone.utc),
    }
    if title:
        update["title"] = title[:120]
    _chat_col().update_one({"session_id": session_id}, {"$set": update})


def get_chat_sessions() -> list[dict]:
    """Return session headers (no messages) sorted newest first."""
    sessions = list(
        _chat_col()
        .find({}, {"_id": 0, "messages": 0})
        .sort("updated_at", DESCENDING)
    )
    # Serialise datetime to ISO string for JSON
    for s in sessions:
        for key in ("created_at", "updated_at"):
            if isinstance(s.get(key), datetime):
                s[key] = s[key].isoformat()
    return sessions


def get_chat_session(session_id: str) -> dict | None:
    """Return full session including messages."""
    s = _chat_col().find_one({"session_id": session_id}, {"_id": 0})
    if not s:
        return None
    for key in ("created_at", "updated_at"):
        if isinstance(s.get(key), datetime):
            s[key] = s[key].isoformat()
    return s


def delete_chat_session(session_id: str) -> None:
    _chat_col().delete_one({"session_id": session_id})


def rename_chat_session(session_id: str, title: str) -> None:
    """Update only the title of a session — messages are untouched."""
    _chat_col().update_one(
        {"session_id": session_id},
        {"$set": {"title": title[:120], "updated_at": datetime.now(timezone.utc)}},
    )


def audit_summary() -> dict:
    pipeline = [
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    status_counts = {
        r["_id"]: r["count"]
        for r in _audit_col().aggregate(pipeline)
    }
    total        = _audit_col().count_documents({})
    unverified   = _audit_col().count_documents({"source_verified": False})
    flagged      = _audit_col().count_documents(
        {"status": {"$in": ["failed", "scanned"]}}
    )
    return {
        "total":          total,
        "status_counts":  status_counts,
        "unverified":     unverified,
        "flagged":        flagged,
    }
