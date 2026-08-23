"""
offers_db.py — Track individual offers per agent for dashboard history
"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")


def init_offers_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            address TEXT,
            price INTEGER,
            down_pct REAL,
            close_days INTEGER,
            filename TEXT,
            created_at TEXT NOT NULL,
            mls_json TEXT DEFAULT ''
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_offers_phone ON offers(phone)")
    # Columns added after the table's initial creation -- ALTER for
    # existing rows since CREATE TABLE IF NOT EXISTS won't add them.
    for ddl in (
        "ALTER TABLE offers ADD COLUMN mls_json TEXT DEFAULT ''",
        "ALTER TABLE offers ADD COLUMN generator_version TEXT DEFAULT ''",
        "ALTER TABLE offers ADD COLUMN financing_type TEXT DEFAULT ''",
        "ALTER TABLE offers ADD COLUMN thread_status TEXT DEFAULT 'pending'",
        "ALTER TABLE offers ADD COLUMN thread_responded_at TEXT DEFAULT ''",
        "ALTER TABLE offers ADD COLUMN email_sent_at TEXT DEFAULT ''",
        "ALTER TABLE offers ADD COLUMN email_sent_to TEXT DEFAULT ''",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def record_offer(phone: str, parsed: dict, filename: str):
    from pdf_filler import GENERATOR_VERSION

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    price = parsed.get("price", 0)
    down_pct = parsed.get("down_payment_pct", 0)
    close_days = parsed.get("close_days", 0)
    address = parsed.get("address", "")
    financing_type = parsed.get("financing_type", "") or ""
    mls = json.dumps({k: parsed.get(k, 0) for k in ("bed", "bath", "sqft", "year_built", "lot_sqft", "listing_price", "property_type")})
    existing = cursor.execute("SELECT id, price FROM offers WHERE filename = ?", (filename,)).fetchone()
    if existing and not existing[1]:
        cursor.execute("""
            UPDATE offers SET phone=?, address=?, price=?, down_pct=?, close_days=?, mls_json=?, generator_version=?, financing_type=?, created_at=?
            WHERE id=?
        """, (phone, address, price, down_pct, close_days, mls, GENERATOR_VERSION, financing_type, now, existing[0]))
    elif not existing:
        cursor.execute("""
            INSERT INTO offers (phone, address, price, down_pct, close_days, filename, mls_json, generator_version, financing_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (phone, address, price, down_pct, close_days, filename, mls, GENERATOR_VERSION, financing_type, now))
    conn.commit()
    conn.close()


def get_offer_by_filename(filename: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, price, down_pct, close_days, filename, mls_json, generator_version, financing_type, thread_status, thread_responded_at, email_sent_at, email_sent_to, created_at
        FROM offers WHERE filename = ?
    """, (filename,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    if result.get("mls_json"):
        result["mls"] = json.loads(result["mls_json"])
    else:
        result["mls"] = {}
    return result


def record_thread_response(filename: str, action: str) -> bool:
    """First-response-wins: only transitions 'pending' -> action. Returns
    True if this call recorded it, False if the offer had already been
    responded to (caller uses this to suppress a duplicate SMS notification
    on re-clicks / stale tabs)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        UPDATE offers SET thread_status=?, thread_responded_at=?
        WHERE filename=? AND thread_status='pending'
    """, (action, now, filename))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def record_email_sent(filename: str, to_email: str):
    """Latest-send-wins (unlike record_thread_response) -- an agent may
    legitimately resend to a corrected address, and there's no
    duplicate-notification side effect to guard against here like there is
    for thread responses, so just record the most recent send."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        UPDATE offers SET email_sent_at=?, email_sent_to=? WHERE filename=?
    """, (now, to_email, filename))
    conn.commit()
    conn.close()


def get_offers_for_phone(phone: str, limit: int = 50) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, address, price, down_pct, close_days, filename, created_at, thread_status, thread_responded_at
        FROM offers
        WHERE phone = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (phone, limit))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def init_amendments_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS amendments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            offer_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            field TEXT NOT NULL,
            value INTEGER NOT NULL,
            filename TEXT,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_amendments_offer ON amendments(offer_id)")
    conn.commit()
    conn.close()


def record_amendment(offer_id: int, phone: str, field: str, value: int, filename: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO amendments (offer_id, phone, field, value, filename, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (offer_id, phone, field, value, filename, now))
    conn.commit()
    conn.close()


def get_amendments_for_phone(phone: str) -> list:
    """Grouped by offer_id so dashboard() can nest each amendment under its
    original offer row."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, offer_id, field, value, filename, created_at
        FROM amendments
        WHERE phone = ?
        ORDER BY created_at DESC
    """, (phone,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    by_offer = {}
    for r in rows:
        by_offer.setdefault(r["offer_id"], []).append(r)
    return by_offer


init_offers_table()
init_amendments_table()
