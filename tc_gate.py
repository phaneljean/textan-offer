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
            created_at TEXT NOT NULL,
            email_captured_at TEXT,
            followup_sent INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Columns added after the table's initial release -- ALTER TABLE has no
    # IF NOT EXISTS guard in SQLite, so on an already-migrated DB this just
    # fails with "duplicate column" and is ignored.
    for ddl in (
        "ALTER TABLE tc_check_clients ADD COLUMN email_captured_at TEXT",
        "ALTER TABLE tc_check_clients ADD COLUMN followup_sent INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
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
            "UPDATE tc_check_clients SET email = ?, email_captured_at = datetime('now') WHERE client_id = ?",
            (email, client_id),
        )
        conn.commit()
    finally:
        conn.close()


def clients_needing_followup(days: int, limit: int = 200) -> list:
    """Clients who gave an email >= `days` ago and haven't gotten the
    follow-up nudge yet. Used by tc_nudge.py's day-3 re-engagement email."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT client_id, email FROM tc_check_clients "
            "WHERE email IS NOT NULL AND followup_sent = 0 "
            "AND email_captured_at IS NOT NULL "
            "AND email_captured_at <= datetime('now', ?) "
            "LIMIT ?",
            (f"-{int(days)} days", limit),
        ).fetchall()
        return [{"client_id": r[0], "email": r[1]} for r in rows]
    finally:
        conn.close()


def mark_followup_sent(client_id: str) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE tc_check_clients SET followup_sent = 1 WHERE client_id = ?",
            (client_id,),
        )
        conn.commit()
    finally:
        conn.close()
