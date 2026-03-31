"""
Phase 3 - Human Review Tool
============================
Interactive CLI for authorised SECP users to review flagged extractions,
correct errors, and approve records in the knowledge database.

Commands
--------
  python review_tool.py stats                    show database statistics
  python review_tool.py list                     list all records
  python review_tool.py list --status flagged    filter by status
  python review_tool.py review <doc_id>          review a specific document
  python review_tool.py review --next            review lowest-confidence field
  python review_tool.py edit <doc_id> <field>    directly edit one field
  python review_tool.py approve <doc_id>         mark document as approved
  python review_tool.py history <doc_id>         show edit history
  python review_tool.py export                   export to JSON + CSV
  python review_tool.py show <doc_id>            display all extracted fields

Environment
-----------
  Set SECP_REVIEWER=<your name> before running to tag edits with your name.
  If not set, the tool will prompt once at startup.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Optional

import config
from knowledge_db import KnowledgeDB, ALL_FIELDS

W = 70   # display width

# ==============================================================================
# Reviewer identity
# ==============================================================================

_reviewer: Optional[str] = None


def get_reviewer() -> str:
    global _reviewer
    if _reviewer:
        return _reviewer
    _reviewer = os.getenv("SECP_REVIEWER", "").strip()
    if not _reviewer:
        _reviewer = input("  Reviewer name: ").strip() or "unknown"
    return _reviewer


# ==============================================================================
# Display helpers
# ==============================================================================

def _rule(char: str = "=") -> str:
    return char * W


def _wrap(text: str, indent: int = 4) -> str:
    lines = textwrap.wrap(str(text), width=W - indent)
    pad   = " " * indent
    return ("\n" + pad).join(lines)


def _conf_label(score: float) -> str:
    if score >= 0.8:
        return f"[{score:.2f}] HIGH"
    elif score >= config.CONFIDENCE_REVIEW_THRESHOLD:
        return f"[{score:.2f}] OK"
    elif score >= 0.4:
        return f"[{score:.2f}] LOW  <-- REVIEW"
    else:
        return f"[{score:.2f}] VERY LOW  <-- REVIEW"


def _fmt_value(val: Any) -> str:
    if val is None:
        return "(not extracted)"
    if isinstance(val, list):
        if not val:
            return "(empty list)"
        return "\n    ".join(f"- {item}" for item in val)
    if isinstance(val, dict):
        return json.dumps(val, indent=2)
    return str(val)


def _print_field_block(
    field: str,
    value: Any,
    conf_info: dict,
    show_excerpt: bool = True,
) -> None:
    conf  = conf_info.get("confidence", 0.0)
    label = _conf_label(conf)
    print(f"\n  Field     : {field}")
    print(f"  Confidence: {label}")
    print(f"  Value     :")
    val_str = _fmt_value(value)
    for line in val_str.split("\n"):
        print(f"    {line}")
    if show_excerpt and conf_info.get("excerpt"):
        excerpt = conf_info["excerpt"][:300].replace("\n", " ")
        print(f"  Excerpt   : ...{excerpt}...")


# ==============================================================================
# Commands
# ==============================================================================

def cmd_stats(db: KnowledgeDB) -> None:
    st = db.stats()
    print(f"\n{_rule()}")
    print("  SECP Knowledge Database  |  Statistics")
    print(_rule())
    print(f"  {'Total records':<28}: {st['total_records']}")
    print(f"  {'Pending review':<28}: {st['pending_review']}")
    print(f"  {'Human reviewed':<28}: {st['human_reviewed']}")
    print(f"  {'Approved':<28}: {st['approved']}")
    print(f"  {'Open review queue items':<28}: {st['open_queue_items']}")
    print(f"  {'Average confidence':<28}: {st['avg_confidence']:.2f}")

    flagged = db.get_flagged_docs()
    if flagged:
        print(f"\n  Documents with flagged fields ({len(flagged)}):")
        print(f"  {'Filename':<52} {'Flagged':>7}  {'MinConf':>7}")
        print("  " + "-" * 68)
        for d in flagged:
            print(f"  {d['filename'][:50]:<52} {d['flagged_count']:>7}  "
                  f"{d['min_confidence']:>7.2f}")
    print(_rule())


def cmd_list(db: KnowledgeDB, status_filter: Optional[str] = None) -> None:
    orders = db.get_all_orders()
    if status_filter:
        orders = [o for o in orders if o["review_status"] == status_filter]

    print(f"\n{_rule()}")
    print(f"  Records{' (filter: ' + status_filter + ')' if status_filter else ''}")
    print(_rule())
    print(f"  {'#':<4} {'Filename':<48} {'Status':<12} {'Version':>7}")
    print("  " + "-" * 72)
    for i, o in enumerate(orders, 1):
        print(f"  {i:<4} {o['filename'][:46]:<48} "
              f"{o['review_status']:<12} v{o['extraction_version']:>4}")
        print(f"       doc_id: {o['doc_id'][:40]}")
    print(_rule())
    print(f"  {len(orders)} record(s)")


def cmd_show(db: KnowledgeDB, doc_id: str) -> None:
    order = db.get_order(doc_id)
    if not order:
        print(f"  Not found: {doc_id}")
        return
    conf_map = db.get_field_confidence(doc_id)

    print(f"\n{_rule()}")
    print(f"  {order['filename']}")
    print(f"  doc_id : {doc_id}")
    print(f"  status : {order['review_status']}  |  version: {order['extraction_version']}")
    print(_rule())

    for field in ALL_FIELDS:
        val       = order.get(field)
        conf_info = conf_map.get(field, {"confidence": 0.0})
        conf      = conf_info.get("confidence", 0.0)
        flag_str  = "  <-- REVIEW" if conf < config.CONFIDENCE_REVIEW_THRESHOLD else ""
        print(f"\n  {field}")
        print(f"    Confidence : {conf:.2f}{flag_str}")
        val_str = _fmt_value(val)
        for line in val_str.split("\n"):
            print(f"    {line}")

    print(f"\n{_rule()}")


def cmd_review(db: KnowledgeDB, doc_id: Optional[str], next_mode: bool) -> None:
    reviewer = get_reviewer()

    if next_mode:
        queue = db.get_review_queue()
        if not queue:
            print("  Review queue is empty. Nothing to review.")
            return
        # Pop the field with lowest confidence
        item   = queue[0]
        doc_id = item["doc_id"]
        print(f"\n  Next in queue: {item['filename']}")
        print(f"  Field: {item['field_name']} (confidence: {item['confidence']:.2f})")

    if not doc_id:
        print("  Provide a doc_id or use --next")
        return

    order = db.get_order(doc_id)
    if not order:
        print(f"  Not found: {doc_id}")
        return

    conf_map = db.get_field_confidence(doc_id)
    flagged_queue = db.get_review_queue(doc_id)
    flagged_fields = [q["field_name"] for q in flagged_queue]

    print(f"\n{_rule()}")
    print(f"  REVIEW: {order['filename']}")
    print(f"  Status : {order['review_status']}  |  v{order['extraction_version']}")
    print(f"  Reviewer: {reviewer}")
    if flagged_fields:
        print(f"  Flagged fields ({len(flagged_fields)}): {', '.join(flagged_fields)}")
    print(_rule())

    if not flagged_fields:
        print("  No flagged fields. Showing full record for review.")
        fields_to_review = ALL_FIELDS
    else:
        fields_to_review = flagged_fields

    full_text = _load_full_text(doc_id)
    reviewed_count = 0

    for i, field in enumerate(fields_to_review, 1):
        val       = order.get(field)
        conf_info = conf_map.get(field, {"confidence": 0.0})

        print(f"\n  [{i}/{len(fields_to_review)}] ", end="")
        _print_field_block(field, val, conf_info)

        while True:
            choice = input(
                "\n  [A]ccept  [E]dit  [S]kip  [C]ontext  [Q]uit  > "
            ).strip().upper()

            if choice == "A":
                db.update_field(
                    doc_id, field, val,
                    changed_by=reviewer,
                    change_reason="accepted as-is during review"
                )
                print(f"  Accepted.")
                reviewed_count += 1
                break

            elif choice == "E":
                new_val = _prompt_edit(field, val)
                if new_val is not None:
                    reason = input("  Reason for edit (optional): ").strip()
                    db.update_field(
                        doc_id, field, new_val,
                        changed_by=reviewer,
                        change_reason=reason or "manual correction"
                    )
                    print(f"  Updated.")
                    reviewed_count += 1
                else:
                    print("  Edit cancelled.")
                break

            elif choice == "S":
                print("  Skipped.")
                break

            elif choice == "C":
                _show_context(full_text, conf_info)

            elif choice == "Q":
                print(f"\n  Session ended. {reviewed_count} field(s) reviewed.")
                return

            else:
                print("  Invalid choice.")

    print(f"\n{_rule('-')}")
    print(f"  {reviewed_count} field(s) reviewed.")
    if reviewed_count > 0:
        approve = input("  Mark this document as approved? [y/N] > ").strip().upper()
        if approve == "Y":
            db.approve_order(doc_id, reviewer)
            print("  Document approved.")
    print(_rule())


def _prompt_edit(field: str, current: Any) -> Optional[Any]:
    """Prompt the user for a new value, respecting the field's expected type."""
    LIST_FIELDS = {
        "entity_names", "individual_respondents", "date_of_hearing",
        "violations", "action_types", "key_facts",
    }

    print(f"\n  Editing: {field}")
    print(f"  Current value:")
    for line in _fmt_value(current).split("\n"):
        print(f"    {line}")

    if field in LIST_FIELDS:
        print("  Enter items one per line. Blank line to finish. 'CANCEL' to abort.")
        items = []
        while True:
            line = input(f"    [{len(items)+1}] ").strip()
            if line.upper() == "CANCEL":
                return None
            if not line:
                break
            items.append(line)
        return items if items else current

    elif field == "penalty_pkr":
        raw = input("  Enter numeric value (or blank to cancel): ").strip()
        if not raw:
            return None
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            print("  Invalid number.")
            return None

    elif field == "legal_provisions":
        print("  Enter as JSON array, e.g. [{\"section\":\"510\",\"act\":\"Companies Act, 2017\",\"clause\":null}]")
        print("  Or blank to cancel:")
        raw = input("  > ").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            print("  Invalid JSON.")
            return None

    else:
        print("  Enter new value (blank to cancel):")
        raw = input("  > ").strip()
        return raw if raw else None


def _show_context(full_text: Optional[str], conf_info: dict) -> None:
    if not full_text:
        print("  (source text not available)")
        return
    start = conf_info.get("char_start")
    if start is not None:
        ctx_start = max(0, start - 250)
        ctx_end   = min(len(full_text), start + 500)
        print(f"\n  Source context (chars {ctx_start}-{ctx_end}):")
        print("  " + "-" * 60)
        snippet = full_text[ctx_start:ctx_end].replace("\n", " ")
        for line in textwrap.wrap(snippet, width=W - 4):
            print(f"    {line}")
        print("  " + "-" * 60)
    elif conf_info.get("excerpt"):
        print(f"\n  Excerpt: {conf_info['excerpt'][:500]}")
    else:
        print("  (no source location recorded for this field)")


def _load_full_text(doc_id: str) -> Optional[str]:
    for p in config.PROCESSED_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("doc_id") == doc_id:
                return d.get("full_text", "")
        except Exception:
            pass
    return None


def cmd_edit(db: KnowledgeDB, doc_id: str, field: str) -> None:
    if field not in ALL_FIELDS:
        print(f"  Unknown field '{field}'. Valid fields: {', '.join(ALL_FIELDS)}")
        return
    order = db.get_order(doc_id)
    if not order:
        print(f"  Not found: {doc_id}")
        return
    reviewer = get_reviewer()
    conf_map = db.get_field_confidence(doc_id)
    _print_field_block(field, order.get(field), conf_map.get(field, {}))
    new_val = _prompt_edit(field, order.get(field))
    if new_val is not None:
        reason = input("  Reason (optional): ").strip()
        db.update_field(doc_id, field, new_val,
                        changed_by=reviewer,
                        change_reason=reason or "manual edit")
        print("  Field updated.")
    else:
        print("  Edit cancelled.")


def cmd_approve(db: KnowledgeDB, doc_id: str) -> None:
    reviewer = get_reviewer()
    if db.approve_order(doc_id, reviewer):
        print(f"  Approved: {doc_id}")
    else:
        print(f"  Not found: {doc_id}")


def cmd_history(db: KnowledgeDB, doc_id: str) -> None:
    history = db.get_field_history(doc_id)
    if not history:
        print(f"  No history for: {doc_id}")
        return
    print(f"\n{_rule()}")
    print(f"  Edit History: {doc_id[:50]}")
    print(_rule())
    for h in history:
        print(f"\n  {h['changed_at'][:19]}  |  {h['changed_by']}  |  {h['field_name']}")
        if h.get("change_reason"):
            print(f"    Reason  : {h['change_reason']}")
        old_str = str(h['old_value'])[:80] if h['old_value'] else "(none)"
        new_str = str(h['new_value'])[:80] if h['new_value'] else "(none)"
        print(f"    Before  : {old_str}")
        print(f"    After   : {new_str}")
    print(_rule())


def cmd_export(db: KnowledgeDB) -> None:
    json_path = db.export_json()
    csv_path  = db.export_csv()
    print(f"  Exported JSON: {json_path}")
    print(f"  Exported CSV : {csv_path}")


# ==============================================================================
# Entry point
# ==============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="SECP Phase 3 - Human Review Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("stats",   help="Show database statistics")
    sub.add_parser("export",  help="Export to JSON and CSV")

    p_list = sub.add_parser("list", help="List all records")
    p_list.add_argument("--status", choices=["pending","reviewed","approved","flagged"],
                        default=None)

    p_show = sub.add_parser("show", help="Display all extracted fields for a document")
    p_show.add_argument("doc_id")

    p_review = sub.add_parser("review", help="Interactive review session")
    p_review.add_argument("doc_id", nargs="?", default=None)
    p_review.add_argument("--next", action="store_true",
                           help="Review next item from queue")

    p_edit = sub.add_parser("edit", help="Edit a single field")
    p_edit.add_argument("doc_id")
    p_edit.add_argument("field")

    p_approve = sub.add_parser("approve", help="Mark document as approved")
    p_approve.add_argument("doc_id")

    p_history = sub.add_parser("history", help="Show edit history for a document")
    p_history.add_argument("doc_id")

    args = ap.parse_args()

    if not args.command:
        ap.print_help()
        return

    db = KnowledgeDB()

    if args.command == "stats":
        cmd_stats(db)
    elif args.command == "list":
        cmd_list(db, getattr(args, "status", None))
    elif args.command == "show":
        cmd_show(db, args.doc_id)
    elif args.command == "review":
        cmd_review(db, getattr(args, "doc_id", None), getattr(args, "next", False))
    elif args.command == "edit":
        cmd_edit(db, args.doc_id, args.field)
    elif args.command == "approve":
        cmd_approve(db, args.doc_id)
    elif args.command == "history":
        cmd_history(db, args.doc_id)
    elif args.command == "export":
        cmd_export(db)


if __name__ == "__main__":
    main()
