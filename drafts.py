"""
drafts.py — Holds one pending offer per phone between the AI Offer Builder's
confirmation step ("Everything looks good. Create offer?") and the agent's
YES/NO reply.

Stored in SQLite rather than an in-memory dict: Railway/gunicorn can run
multiple worker processes, and the agent's YES may land on a different
worker than the one that showed the confirmation. Same reasoning as every
other piece of state in this app.
"""
import os
import sqlite3
import json
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
DRAFT_TTL_MINUTES = int(os.environ.get("DRAFT_TTL_MINUTES", 30))


def init_drafts_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_drafts (
            phone TEXT PRIMARY KEY,
            parsed_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_draft(phone: str, parsed: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO pending_drafts (phone, parsed_json, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET parsed_json=excluded.parsed_json, created_at=excluded.created_at
    """, (phone, json.dumps(parsed), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_draft(phone: str) -> dict:
    """Returns the pending parsed-offer dict, or None if there isn't one or
    it expired (agent went quiet mid-conversation -- don't resurrect a stale
    draft hours or days later against a YES that was actually about something
    else)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT parsed_json, created_at FROM pending_drafts WHERE phone = ?", (phone,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    created = datetime.fromisoformat(row["created_at"])
    if datetime.utcnow() - created > timedelta(minutes=DRAFT_TTL_MINUTES):
        clear_draft(phone)
        return None
    return json.loads(row["parsed_json"])


def clear_draft(phone: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM pending_drafts WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()


init_drafts_table()
