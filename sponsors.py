"""
sponsors.py — Title company (or lender) sponsor placement on the generated
PDF packet.

The pitch: a title rep in a specific Texas market pays for their branding
to appear on every offer TxtAnOffer generates for a property in their
county. Matching is by county, not brokerage or statewide, because title
company relationships are regional in practice -- a Houston-area rep has
no reason to pay for placement on a Dallas contract they can't service.

Provisioning is by hand (see create_sponsor), same high-touch pattern as
brokerages.py. There is no enforced exclusivity at the database level --
selling the same county to two sponsors at once would defeat the pitch, so
that discipline lives in whoever runs /admin/sponsors, not in code.
"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")


def init_sponsors_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS title_sponsors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tagline TEXT,
            contact_phone TEXT,
            contact_email TEXT,
            counties TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _normalize_county(county: str) -> str:
    return (county or "").strip().lower().replace(" county", "")


def create_sponsor(name: str, counties: list, tagline: str = "", contact_phone: str = "", contact_email: str = "") -> dict:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    counties_json = json.dumps([_normalize_county(c) for c in counties if c.strip()])
    cursor.execute(
        "INSERT INTO title_sponsors (name, tagline, contact_phone, contact_email, counties, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (name, tagline, contact_phone, contact_email, counties_json, now),
    )
    conn.commit()
    sponsor_id = cursor.lastrowid
    conn.close()
    return get_sponsor(sponsor_id)


def get_sponsor(sponsor_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM title_sponsors WHERE id = ?", (sponsor_id,)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["counties"] = json.loads(d["counties"])
    return d


def list_sponsors() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM title_sponsors ORDER BY created_at DESC").fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["counties"] = json.loads(d["counties"])
        results.append(d)
    return results


def set_sponsor_active(sponsor_id: int, active: bool):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE title_sponsors SET active = ? WHERE id = ?", (1 if active else 0, sponsor_id))
    conn.commit()
    conn.close()


def get_sponsor_for_county(county: str):
    """First active sponsor whose county list contains this county
    (case-insensitive, "County" suffix optional). None if no county was
    parsed for this offer, or no sponsor covers it yet -- both silent,
    non-blocking outcomes; this must never stop a contract from
    generating."""
    normalized = _normalize_county(county)
    if not normalized:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT * FROM title_sponsors WHERE active = 1 ORDER BY created_at ASC"
    ).fetchall()
    conn.close()
    for row in rows:
        d = dict(row)
        counties = json.loads(d["counties"])
        if normalized in counties:
            d["counties"] = counties
            return d
    return None


init_sponsors_table()
