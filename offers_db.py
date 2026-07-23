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
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_offers_phone ON offers(phone)")
    conn.commit()
    conn.close()


def record_offer(phone: str, parsed: dict, filename: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    cursor.execute("""
        INSERT INTO offers (phone, address, price, down_pct, close_days, filename, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        phone,
        parsed.get("address", ""),
        parsed.get("price", 0),
        parsed.get("down_payment_pct", 0),
        parsed.get("close_days", 0),
        filename,
        now,
    ))
    conn.commit()
    conn.close()


def get_offer_by_filename(filename: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, phone, address, price, down_pct, close_days, filename, created_at
        FROM offers WHERE filename = ?
    """, (filename,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


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


init_offers_table()
