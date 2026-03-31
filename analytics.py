"""
analytics.py — Trend & Pattern Identification for SECP Adjudication Orders
Runs MongoDB aggregation pipelines to produce descriptive statistics.
All outputs are factual and descriptive only — no predictions or interpretations.
"""

from __future__ import annotations
from typing import Optional
import mongo_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_match(parsed: dict) -> dict:
    """Build a MongoDB $match stage that mirrors search engine filters."""
    match: dict = {}

    # Date range
    date_from = parsed.get("date_from")
    date_to   = parsed.get("date_to")
    if date_from or date_to:
        date_filter: dict = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        match["extracted_metadata.order_date"] = date_filter

    # Act filter
    acts = parsed.get("acts") or []
    if acts:
        patterns = [{"extracted_metadata.legal_provisions.act": {"$regex": a, "$options": "i"}} for a in acts]
        match["$or"] = patterns

    # Entity category
    ec = parsed.get("entity_category")
    if ec:
        match["extracted_metadata.entity_category"] = {"$regex": ec, "$options": "i"}

    # Penalty range
    pmin = parsed.get("penalty_min")
    pmax = parsed.get("penalty_max")
    if pmin is not None or pmax is not None:
        pen_filter: dict = {}
        if pmin is not None:
            pen_filter["$gte"] = pmin
        if pmax is not None:
            pen_filter["$lte"] = pmax
        match["extracted_metadata.penalty_pkr"] = pen_filter

    return match


def _year(date_field: str) -> dict:
    """Extract year string from a YYYY-MM-DD field."""
    return {"$substr": [{"$ifNull": [date_field, ""]}, 0, 4]}


# ---------------------------------------------------------------------------
# Aggregation pipelines
# ---------------------------------------------------------------------------

def _temporal_by_year(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$match": {"extracted_metadata.order_date": {"$regex": r"^\d{4}"}}},
        {"$addFields": {"_year": _year("$extracted_metadata.order_date")}},
        {"$group": {
            "_id":         "$_year",
            "count":       {"$sum": 1},
            "total_pen":   {"$sum": {"$ifNull": ["$extracted_metadata.penalty_pkr", 0]}},
            "penalty_cases": {"$sum": {"$cond": [{"$gt": ["$extracted_metadata.penalty_pkr", 0]}, 1, 0]}},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id":      0,
            "year":     "$_id",
            "count":    1,
            "total_penalty_pkr": "$total_pen",
            "penalty_cases": 1,
        }},
    ]
    return list(col.aggregate(pipeline))


def _by_action_type(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$unwind": {"path": "$extracted_metadata.action_types", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id":   "$extracted_metadata.action_types",
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "action_type": "$_id", "count": 1}},
    ]
    return list(col.aggregate(pipeline))


def _by_entity_category(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id":   {"$ifNull": ["$extracted_metadata.entity_category", "Unclassified"]},
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "entity_category": "$_id", "count": 1}},
    ]
    return list(col.aggregate(pipeline))


def _by_legal_act(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$unwind": {"path": "$extracted_metadata.legal_provisions", "preserveNullAndEmptyArrays": False}},
        {"$match": {"extracted_metadata.legal_provisions.act": {"$ne": None, "$ne": ""}}},
        {"$group": {
            "_id":   "$extracted_metadata.legal_provisions.act",
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 15},
        {"$project": {"_id": 0, "act": "$_id", "count": 1}},
    ]
    return list(col.aggregate(pipeline))


def _by_section(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$unwind": {"path": "$extracted_metadata.legal_provisions", "preserveNullAndEmptyArrays": False}},
        {"$match": {
            "extracted_metadata.legal_provisions.section": {"$ne": None, "$ne": ""},
            "extracted_metadata.legal_provisions.act":     {"$ne": None, "$ne": ""},
        }},
        {"$group": {
            "_id": {
                "act":     "$extracted_metadata.legal_provisions.act",
                "section": "$extracted_metadata.legal_provisions.section",
            },
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 15},
        {"$project": {
            "_id": 0,
            "act":     "$_id.act",
            "section": "$_id.section",
            "count":   1,
        }},
    ]
    return list(col.aggregate(pipeline))


def _top_violations(col, match: dict) -> list[dict]:
    pipeline = [
        {"$match": match},
        {"$unwind": {"path": "$extracted_metadata.violations", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id":   "$extracted_metadata.violations",
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 10},
        {"$project": {"_id": 0, "violation": "$_id", "count": 1}},
    ]
    return list(col.aggregate(pipeline))


def _by_category(col, match: dict) -> list[dict]:
    """Distribution by document category (Companies Act 2017 / Companies Rules etc.)."""
    pipeline = [
        {"$match": match},
        {"$group": {
            "_id":   {"$ifNull": ["$category", "Uncategorised"]},
            "count": {"$sum": 1},
        }},
        {"$sort": {"count": -1}},
        {"$project": {"_id": 0, "category": "$_id", "count": 1}},
    ]
    return list(col.aggregate(pipeline))


def _penalty_summary(col, match: dict) -> dict:
    pipeline = [
        {"$match": match},
        {"$match": {"extracted_metadata.penalty_pkr": {"$gt": 0}}},
        {"$group": {
            "_id":    None,
            "count":  {"$sum": 1},
            "total":  {"$sum": "$extracted_metadata.penalty_pkr"},
            "avg":    {"$avg": "$extracted_metadata.penalty_pkr"},
            "min":    {"$min": "$extracted_metadata.penalty_pkr"},
            "max":    {"$max": "$extracted_metadata.penalty_pkr"},
        }},
    ]
    rows = list(col.aggregate(pipeline))
    if not rows:
        return {}
    r = rows[0]
    return {
        "cases_with_penalty": int(r["count"]),
        "total_pkr":          int(r["total"]),
        "avg_pkr":            int(r["avg"]),
        "min_pkr":            int(r["min"]),
        "max_pkr":            int(r["max"]),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_analytics(parsed: dict) -> dict:
    """
    Run all relevant analytics pipelines for the given parsed query.
    Returns a structured dict ready for the API response.
    """
    col   = mongo_store.get_db()["documents"]
    match = _base_match(parsed)

    # Scope count
    total = col.count_documents(match or {})

    return {
        "scope_total":       total,
        "by_year":           _temporal_by_year(col, match),
        "by_action_type":    _by_action_type(col, match),
        "by_entity_category": _by_entity_category(col, match),
        "by_legal_act":      _by_legal_act(col, match),
        "by_section":        _by_section(col, match),
        "top_violations":    _top_violations(col, match),
        "by_category":       _by_category(col, match),
        "penalty_summary":   _penalty_summary(col, match),
    }
