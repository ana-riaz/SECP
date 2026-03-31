"""
Central configuration for the SECP RAG pipeline.
All paths, thresholds, and environment variables live here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


BASE_DIR       = Path(__file__).parent
DOCS_DIR       = BASE_DIR / "SECP_docs"          
DATA_DIR       = BASE_DIR / "data"
PROCESSED_DIR  = DATA_DIR / "processed_docs"    
FLAGGED_DIR    = DATA_DIR / "flagged"            
AUDIT_LOG_PATH = DATA_DIR / "audit_log.jsonl"    
CHECKSUMS_PATH = DATA_DIR / "checksums.json"    
VALID_SOURCE_DOMAIN  = "secp.gov.pk"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}
MIN_CHARS_PER_PAGE   = 50    # pages below this are considered scanned/image
SCANNED_DOC_RATIO    = 0.5   # flag whole doc if >50 % of pages are image-only

# ── OCR settings ─────────────────────────────────────────────────────────────
OCR_DPI = 300    # DPI for pdf2image page rendering (higher = better quality, slower)
METADATA_MODEL = "gpt-4o-mini"   # model used for on-demand metadata extraction
# Path to the Tesseract executable.
TESSERACT_CMD = os.getenv(
    "TESSERACT_CMD",
    r"D:\Softwares\Tesseract-OCR\tesseract.exe",
)

# Poppler bin path for pdf2image on Windows
# Downloaded from: https://github.com/oschwartz10612/poppler-windows/releases
POPPLER_PATH = os.getenv("POPPLER_PATH", None)  # e.g. r"C:\poppler\Library\bin"

# Phase 2 – LLM / Embedding models
EXTRACTION_MODEL  = "gpt-4o-mini"        
QUERY_PARSE_MODEL = "gpt-4o-mini"        # NLP query → structured filters
EMBEDDING_MODEL   = "text-embedding-3-small"   # OpenAI embedding model
EMBEDDING_DIM     = 1536

# Fallback local embedding (sentence-transformers) when no OpenAI key
LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# ── Phase 2 – Chunking 
CHUNK_SIZE    = 800    # characters per chunk
CHUNK_OVERLAP = 150    # overlap between consecutive chunks

# Phase 2 – Qdrant vector store
# Local embedded mode (no Docker): set QDRANT_URL="" or leave unset
# Cloud mode: set QDRANT_URL + QDRANT_API_KEY in .env
QDRANT_PATH       = DATA_DIR / "qdrant"        # local persistent storage path
QDRANT_URL        = os.getenv("QDRANT_URL", "")        # e.g. https://xxx.qdrant.io
QDRANT_API_KEY    = os.getenv("QDRANT_API_KEY", "")
QDRANT_COLLECTION = "secp_orders"

# ── API keys (loaded from .env) ───────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Embedding dimension (must match the model used at index time)
# text-embedding-3-small → 1536  |  all-MiniLM-L6-v2 → 384
EMBEDDING_DIM = 1536 if OPENAI_API_KEY else 384

# ── Phase 2 – Search ─────────────────────────────────────────────────────────
DEFAULT_TOP_K = 10    # default number of results to return

# ── Phase 3 – Structured Knowledge Extraction ────────────────────────────────
KNOWLEDGE_DB_PATH          = DATA_DIR / "knowledge.db"
CONFIDENCE_REVIEW_THRESHOLD = 0.6    # fields below this are flagged for human review
STRUCTURED_EXTRACTION_MODEL = "gpt-4o-mini"
VISION_OCR_MODEL            = "gpt-4o"      # used for scanned-page OCR fallback

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB  = os.getenv("MONGO_DB",  "secp")
