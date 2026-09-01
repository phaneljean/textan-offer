"""
tc_gate.py -- Free-use gate for the anonymous /tc-check tool: FREE_USES
checks per browser (tracked by an httponly client-id cookie set by the
route), then an email is required to keep going. Not an auth system --
once an email is captured for a client id, that browser is unlimited
going forward. This is a lead-gen/product gate, not the abuse guard
(that's the per-IP throttle in rate_limit.py, which still applies
regardless of email status).

Backed by the same SQLite DB as everything else in this app
(subscriptions.db on Railway's persistent volume).
"""
import os
import sqlite3

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
FREE_USES = 3


def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tc_check_clients (
            client_id TEXT PRIMARY KEY,
            use_count INTEGER NOT NULL DEFAULT 0,
            email TEXT,
            created_at TEXT NOT NULL
        )
    """)
    return conn


def get_client(client_id: str) -> dict:
    """Returns {"use_count": int, "email": str|None} for a client id,
    creating the row on first sight."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tc_check_clients (client_id, use_count, email, created_at) "
            "VALUES (?, 0, NULL, datetime('now'))",
            (client_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT use_count, email FROM tc_check_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return {"use_count": row[0], "email": row[1]}
    finally:
        conn.close()


def record_use(client_id: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tc_check_clients SET use_count = use_count + 1 WHERE client_id = ?",
            (client_id,),
        )
        conn.commit()
    finally:
        conn.close()


def save_email(client_id: str, email: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tc_check_clients SET email = ? WHERE client_id = ?",
            (email, client_id),
        )
        conn.commit()
    finally:
        conn.close()
