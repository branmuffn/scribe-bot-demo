"""AI jobs for the worker queue: "Re-run AI" on a conversation, and cloud requests Brian approved in Review.

Both go through the 'ai_rerun' job (a one-stage pipeline registered in scribe_bot/worker.py). The payload says which
conversation and whether Brian approved this run; approved=True is what the router needs for Stage 1/2 tasks.
"""
from __future__ import annotations

import json
import sqlite3

from scribe_bot.ai.extract import extract_conversation
from scribe_bot.core import queue
from scribe_bot.core.db import now

KIND = "ai_rerun"
COMPARE_KIND = "ai_compare"   # Compare with the other model (extract.other_model); keeps both versions


def queue_rerun(conn: sqlite3.Connection, conversation_id: int, approved: bool = False) -> int | None:
    """Queue one AI run for a conversation; None if one is already waiting (no double clicks, no double spend)."""
    for row in conn.execute("SELECT payload FROM job WHERE kind = ? AND status IN ('pending', 'running')", (KIND,)):
        if json.loads(row[0]).get("conversation_id") == conversation_id:
            return None
    return queue.enqueue(conn, KIND, payload={"conversation_id": conversation_id, "approved": approved})


def pending_for(conn: sqlite3.Connection, conversation_id: int) -> bool:
    return any(json.loads(r[0]).get("conversation_id") == conversation_id for r in conn.execute(
        "SELECT payload FROM job WHERE kind = ? AND status IN ('pending', 'running')", (KIND,)))


def rerun_stage(ctx: queue.StageContext) -> str:
    cid = ctx.payload["conversation_id"]
    result = extract_conversation(ctx.conn, cid, approved=bool(ctx.payload.get("approved")))
    if result.get("status") == "pending_approval" and ctx.conn.execute(
            "SELECT 1 FROM review_item WHERE kind = 'approval' AND conversation_id = ? AND status = 'open'",
            (cid,)).fetchone() is None:
        ctx.conn.execute("INSERT INTO review_item (kind, conversation_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                         (cid, json.dumps({"estimate": result.get("estimate")}), now()))
    return json.dumps(result)


PIPELINE: queue.Pipeline = [("extract", rerun_stage)]


def queue_compare(conn: sqlite3.Connection, conversation_id: int) -> int | None:
    """Brian confirmed the cost on the confirm step, so the run is approved. None if one is already waiting."""
    if compare_pending(conn, conversation_id):
        return None
    return queue.enqueue(conn, COMPARE_KIND, payload={"conversation_id": conversation_id})


def compare_pending(conn: sqlite3.Connection, conversation_id: int) -> bool:
    return any(json.loads(r[0]).get("conversation_id") == conversation_id for r in conn.execute(
        "SELECT payload FROM job WHERE kind = ? AND status IN ('pending', 'running')", (COMPARE_KIND,)))


def compare_stage(ctx: queue.StageContext) -> str:
    from scribe_bot.ai.extract import other_model
    cid = ctx.payload["conversation_id"]
    return json.dumps(extract_conversation(ctx.conn, cid, approved=True, chain=[other_model(ctx.conn, cid)], keep=False))


COMPARE_PIPELINE: queue.Pipeline = [("compare", compare_stage)]


DETAIL_KIND = "ai_detail"   # Detailed notes (Brian confirmed the cost)


def queue_detail(conn: sqlite3.Connection, conversation_id: int) -> int | None:
    if detail_pending(conn, conversation_id):
        return None
    return queue.enqueue(conn, DETAIL_KIND, payload={"conversation_id": conversation_id})


def detail_pending(conn: sqlite3.Connection, conversation_id: int) -> bool:
    return any(json.loads(r[0]).get("conversation_id") == conversation_id for r in conn.execute(
        "SELECT payload FROM job WHERE kind = ? AND status IN ('pending', 'running')", (DETAIL_KIND,)))


def detail_stage(ctx: queue.StageContext) -> str:
    from scribe_bot.ai.extract import detail_conversation
    return json.dumps(detail_conversation(ctx.conn, ctx.payload["conversation_id"], approved=True))


DETAIL_PIPELINE: queue.Pipeline = [("detail", detail_stage)]
