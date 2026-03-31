"""
Phase 2, Step 2 – Chunking and Vector Embedding (Qdrant)
=========================================================
Splits each document's full_text into overlapping chunks, embeds them, and
stores everything in a persistent Qdrant collection.

Why Qdrant over ChromaDB
────────────────────────
  • Native list payload support   → sections, entity_names, acts stored as real lists
  • Rich payload filters          → MatchValue, MatchText, Range work correctly
  • Embedded local mode           → no Docker required; single directory store
  • Cloud-ready                   → change QdrantClient init, zero code change

Collection  : secp_orders
Each point  : vector + rich payload (list fields stored natively)

Embedding backends
──────────────────
  1. OpenAI text-embedding-3-small   (if OPENAI_API_KEY is set)  → dim 1536
  2. sentence-transformers all-MiniLM-L6-v2  (local fallback)   → dim 384

Usage
─────
  python chunk_embed.py               # index docs not yet indexed
  python chunk_embed.py --force       # reindex everything
  python chunk_embed.py --doc <file>  # single document by filename
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    PayloadSchemaType, TextIndexParams, TokenizerType,
    Filter, FieldCondition, MatchValue, FilterSelector,
)

import config

# Qdrant client factory
# ═════════════════════════════════════════════════

def get_qdrant_client() -> QdrantClient:
    if config.QDRANT_URL:
        return QdrantClient(
            url=config.QDRANT_URL,
            api_key=config.QDRANT_API_KEY or None,
        )
    config.QDRANT_PATH.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(config.QDRANT_PATH))

# Text chunker
# ═════════════════════════════════════════════════

class TextChunker:
    """Splits text into overlapping chunks at natural break points."""

    def __init__(self, chunk_size: int = config.CHUNK_SIZE,
                 overlap: int = config.CHUNK_OVERLAP):
        self.chunk_size = chunk_size
        self.overlap    = overlap

    def split(self, text: str) -> list[str]:
        if not text.strip():
            return []
        text = re.sub(r'\n{3,}', '\n\n', text).strip()

        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + self.chunk_size, len(text))
            if end < len(text):
                for sep in ['\n\n', '\n', '. ', ' ']:
                    pos = text.rfind(sep, start, end)
                    if pos > start + self.chunk_size // 2:
                        end = pos + len(sep)
                        break
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            start = end - self.overlap if end < len(text) else len(text)
        return chunks


# ══════════════════════════════════════════════════════════════════════════════
# Embedding backends
# ══════════════════════════════════════════════════════════════════════════════

class OpenAIEmbedder:
    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)
        self.model  = config.EMBEDDING_MODEL

    def embed(self, texts: list[str]) -> list[list[float]]:
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), 100):
            batch = texts[i : i + 100]
            resp  = self.client.embeddings.create(model=self.model, input=batch)
            all_embeddings.extend([item.embedding for item in resp.data])
        return all_embeddings

    @property
    def dim(self) -> int:
        return 1536


class LocalEmbedder:
    def __init__(self):
        from sentence_transformers import SentenceTransformer
        print(f"  Loading local embedding model: {config.LOCAL_EMBEDDING_MODEL} ...")
        self.model = SentenceTransformer(config.LOCAL_EMBEDDING_MODEL)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, show_progress_bar=False).tolist()

    @property
    def dim(self) -> int:
        return 384


def get_embedder():
    if config.OPENAI_API_KEY:
        try:
            return OpenAIEmbedder()
        except Exception as e:
            print(f"  [WARN] OpenAI embedder failed ({e}), falling back to local.")
    return LocalEmbedder()


# ══════════════════════════════════════════════════════════════════════════════
# Payload builder
# Qdrant supports native list fields — no serialisation needed!
# ══════════════════════════════════════════════════════════════════════════════

def build_payload(doc: dict, chunk_index: int) -> dict:
    fm = doc.get('order_metadata', {})
    em = doc.get('extracted_metadata', {})

    provisions = em.get('legal_provisions', [])

    # Merge section numbers from filename parser + LLM extraction
    sections_filename = fm.get('sections', [])
    sections_llm      = [str(p.get('section', '')) for p in provisions if p.get('section')]
    sections          = list(dict.fromkeys(sections_filename + sections_llm))

    # Act names
    acts = list(dict.fromkeys(p.get('act', '') for p in provisions if p.get('act')))

    # Entity names — prefer LLM result; fall back to filename parser
    entity_names = em.get('entity_names') or [fm.get('company_name', '')]
    entity_names = [e for e in entity_names if e]

    # Penalty — store as float; -1.0 sentinel for "no monetary penalty"
    raw_penalty = em.get('penalty_pkr')
    try:
        penalty_pkr = float(raw_penalty) if raw_penalty is not None else -1.0
    except (TypeError, ValueError):
        penalty_pkr = -1.0

    return {
        # Identity
        "doc_id":       doc['doc_id'],
        "filename":     doc['filename'],
        "chunk_index":  chunk_index,
        "source_url":   doc.get('source_url') or "",

        # Order
        "order_reference": em.get('order_reference') or fm.get('company_name', ''),
        "order_date_iso":  em.get('order_date') or fm.get('order_date_iso') or "",
        "order_date_raw":  fm.get('order_date_raw', ''),
        "order_date_ts":   _iso_to_ts(em.get('order_date') or fm.get('order_date_iso') or ""),

        # Entity / people  (stored as native lists — Qdrant handles this natively)
        "entity_names":           entity_names,
        "individual_respondents": em.get('individual_respondents', []),
        "entity_category":        em.get('entity_category', ''),
        "sector":                 em.get('sector', ''),

        # Legal  (native list fields)
        "sections":         sections,
        "acts":             acts,
        "legal_provisions": provisions,   # list of dicts — fully supported

        # Violations  (native list)
        "violations": em.get('violations', []),

        # Penalty
        "penalty_pkr":  penalty_pkr,
        "penalty_note": em.get('penalty_note') or "",

        # Authority
        "issuing_officer": em.get('issuing_officer') or "",
        "action_types":    em.get('action_types', []),

        # Summary (stored on chunk 0 only)
        "case_summary": em.get('case_summary', '') if chunk_index == 0 else "",

        # Category (folder-level classification)
        "category": doc.get('category', ''),
    }


def _iso_to_ts(date_iso: str) -> int:
    """Convert 'YYYY-MM-DD' to a Unix timestamp integer (0 if unparseable)."""
    try:
        from datetime import datetime, timezone
        return int(datetime.strptime(date_iso, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return 0


def _point_id(doc_id: str, chunk_index: int) -> str:
    """Stable UUID from doc_id + chunk index."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{doc_id}:{chunk_index}"))


# ══════════════════════════════════════════════════════════════════════════════
# Collection setup — creates payload indexes for fast filtering
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_collection(client: QdrantClient, dim: int, force: bool = False) -> None:
    """
    Create collection + payload indexes if they don't already exist.
    If force=True OR the existing collection has a different vector size,
    the collection is deleted and recreated with the correct dimension.
    Note: payload indexes are silently ignored in local embedded mode but
    take effect automatically when switching to Qdrant Cloud/server.
    """
    import warnings
    existing = [c.name for c in client.get_collections().collections]

    if config.QDRANT_COLLECTION in existing:
        # Check if vector dimension matches; if not, force recreate
        info = client.get_collection(config.QDRANT_COLLECTION)
        existing_dim = info.config.params.vectors.size
        if force or existing_dim != dim:
            client.delete_collection(config.QDRANT_COLLECTION)
            print(f"  Deleted old collection (dim={existing_dim}) -> recreating (dim={dim})")
            existing = []

    if config.QDRANT_COLLECTION not in existing:
        client.create_collection(
            collection_name=config.QDRANT_COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"  Created Qdrant collection '{config.QDRANT_COLLECTION}' (dim={dim})")

    # Keyword indexes for exact/list matching
    # (active on Qdrant Cloud/server; silently skipped in local embedded mode)
    for field in ("sections", "acts", "action_types", "entity_category", "sector",
                  "doc_id", "order_date_iso"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
            client.create_payload_index(
                collection_name=config.QDRANT_COLLECTION,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        except Exception:
            pass  # Index already exists

    # Float index for penalty range queries
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
        client.create_payload_index(
            collection_name=config.QDRANT_COLLECTION,
            field_name="penalty_pkr",
            field_schema=PayloadSchemaType.FLOAT,
        )
    except Exception:
        pass

    # Full-text indexes for substring / keyword search on name fields
    for field in ("entity_names", "individual_respondents", "issuing_officer",
                  "order_reference", "violations"):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
            client.create_payload_index(
                collection_name=config.QDRANT_COLLECTION,
                field_name=field,
                field_schema=TextIndexParams(
                    type="text",
                    tokenizer=TokenizerType.WORD,
                    lowercase=True,
                ),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# Indexer
# ══════════════════════════════════════════════════════════════════════════════

class DocumentIndexer:

    def __init__(self, force: bool = False):
        self.client   = get_qdrant_client()
        self.chunker  = TextChunker()
        self.embedder = get_embedder()
        _ensure_collection(self.client, self.embedder.dim, force=force)

    def _doc_already_indexed(self, doc_id: str) -> bool:
        results = self.client.scroll(
            collection_name=config.QDRANT_COLLECTION,
            scroll_filter={"must": [{"key": "doc_id", "match": {"value": doc_id}}]},
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        return len(results[0]) > 0

    def _delete_doc(self, doc_id: str) -> None:
        self.client.delete(
            collection_name=config.QDRANT_COLLECTION,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
                )
            ),
        )

    def index_document(self, doc: dict, force: bool = False) -> int:
        doc_id = doc['doc_id']

        if not force and self._doc_already_indexed(doc_id):
            return 0

        if force:
            self._delete_doc(doc_id)

        full_text = doc.get('full_text', '').strip()
        if not full_text:
            return 0

        chunks = self.chunker.split(full_text)
        if not chunks:
            return 0

        embeddings = self.embedder.embed(chunks)
        points = [
            PointStruct(
                id=_point_id(doc_id, i),
                vector=emb,
                payload=build_payload(doc, i),
            )
            for i, emb in enumerate(embeddings)
        ]

        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=config.QDRANT_COLLECTION,
                points=points[i : i + batch_size],
            )
        return len(points)

    @property
    def total_points(self) -> int:
        return self.client.count(collection_name=config.QDRANT_COLLECTION).count


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline runner
# ══════════════════════════════════════════════════════════════════════════════

def index_all(force: bool = False, target_filename: Optional[str] = None) -> dict[str, int]:
    import mongo_store
    all_docs = mongo_store.get_all_docs()

    if target_filename:
        all_docs = [d for d in all_docs if d.get('filename') == target_filename]

    print(f"\n{'-'*60}")
    print(f"  Qdrant Indexer  |  {len(all_docs)} document(s)")
    print(f"{'-'*60}")

    indexer = DocumentIndexer(force=force)
    results: dict[str, int] = {}

    for doc in all_docs:
        filename = doc['filename']

        if 'extracted_metadata' not in doc:
            print(f"  [!]  {filename[:60]}  WARNING: run extract_metadata.py first")

        chunks = indexer.index_document(doc, force=force)  # doc comes from MongoDB
        if chunks > 0:
            print(f"  [OK] {filename[:60]}  {chunks} chunks indexed")
        else:
            print(f"  [->] {filename[:60]}  skipped (already indexed)")
        results[filename] = chunks

    total_new   = sum(results.values())
    total_store = indexer.total_points
    print(f"\n  Chunks added this run  : {total_new}")
    print(f"  Total points in Qdrant : {total_store}")
    print(f"{'-'*60}\n")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SECP Qdrant Indexer – Phase 2 Step 2")
    parser.add_argument("--force", action="store_true",
                        help="Reindex all documents even if already indexed")
    parser.add_argument("--doc",   type=str, default=None,
                        help="Index only this filename")
    args = parser.parse_args()
    index_all(force=args.force, target_filename=args.doc)
