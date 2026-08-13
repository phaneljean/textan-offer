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
    # Add mls_json column if table already exists without it
    try:
        cursor.execute("ALTER TABLE offers ADD COLUMN mls_json TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def record_offer(phone: str, parsed: dict, filename: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    price = parsed.get("price", 0)
    down_pct = parsed.get("down_payment_pct", 0)
    close_days = parsed.get("close_days", 0)
    address = parsed.get("address", "")
    mls = json.dumps({k: parsed.get(k, 0) for k in ("bed", "bath", "sqft", "year_built", "lot_sqft", "listing_price", "property_type")})
    existing = cursor.execute("SELECT id, price FROM offers WHERE filename = ?", (filename,)).fetchone()
    if existing and not existing[1]:
        cursor.execute("""
            UPDATE offers SET phone=?, address=?, price=?, down_pct=?, close_days=?, mls_json=?, created_at=?
            WHERE id=?
        """, (phone, address, price, down_pct, close_days, mls, now, existing[0]))
    elif not existing:
        cursor.execute("""
            INSERT INTO offers (phone, address, price, down_pct, close_days, filename, mls_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (phone, address, price, down_pct, close_days, filename, mls, now))
    conn.commit()
    conn.close()


def get_offer_by_filename(filename: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, price, down_pct, close_days, filename, mls_json, created_at
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


def get_offers_for_phone(phone: str, limit: int = 50) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, address, price, down_pct, close_days, filename, created_at
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
