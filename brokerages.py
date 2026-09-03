"""
brokerages.py — Minimal multi-tenant layer for the managing-broker product.

A brokerage is the account a managing broker pays for. It doesn't gate
anything on its own -- it's a grouping key: an agent's phone number gets
linked to a brokerage_id either at /signup or, on first use, by leading an
SMS offer with the brokerage's join_code as a prefix (see extract_brokerage_prefix
and app.py's /sms webhook). Once linked, every offer that phone texts in
auto-CCs the brokerage's tc_email with the finished PDF -- no dashboard
login required for that part; /broker/dashboard/<join_code> is the roster
view for a broker who does want to log in and look.

No self-serve signup yet -- brokerages are created by hand (see
create_brokerage), matching the current high-touch B2B sales motion: you
close the broker, then hand them a join code for their roster. join_code
doubles as the dashboard's access secret, so treat it like a password,
not a public slug -- there is no separate login.
"""
import sqlite3
import os
import secrets
import string
from datetime import datetime

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

# Visually ambiguous characters (0/O, 1/I) excluded from generated codes --
# a broker will be reading this off a screen and typing it into a phone.
_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "O0I1")


def init_brokerages_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS brokerages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tc_email TEXT,
            join_code TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _generate_join_code(length: int = 8) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def create_brokerage(name: str, tc_email: str = "") -> dict:
    """Provision a new brokerage account with a fresh join code. Called by
    hand from /admin/brokerages after a broker signs -- there's no
    self-serve flow yet."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    for _ in range(5):
        code = _generate_join_code()
        try:
            cursor.execute(
                "INSERT INTO brokerages (name, tc_email, join_code, created_at) VALUES (?, ?, ?, ?)",
                (name, tc_email, code, now),
            )
            conn.commit()
            brokerage_id = cursor.lastrowid
            conn.close()
            return {"id": brokerage_id, "name": name, "tc_email": tc_email, "join_code": code, "created_at": now}
        except sqlite3.IntegrityError:
            continue  # extremely unlikely code collision -- retry with a new one
    conn.close()
    raise RuntimeError("Could not generate a unique join code after 5 attempts")


def get_brokerage_by_code(join_code: str):
    if not join_code:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT * FROM brokerages WHERE join_code = ?", (join_code.strip().upper(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_brokerage(brokerage_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM brokerages WHERE id = ?", (brokerage_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_brokerages() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = cursor.execute("SELECT * FROM brokerages ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def link_user_to_brokerage(phone: str, brokerage_id: int) -> bool:
    """Returns False if no user row exists yet for this phone (caller should
    create_user() first -- this never creates one itself)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET brokerage_id = ? WHERE phone = ?", (brokerage_id, phone))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def extract_brokerage_prefix(body: str):
    """Look for a brokerage join_code leading an SMS offer, e.g. 'KW123 725k
    3% 21day 104 Main St'. Returns (brokerage_dict, remaining_body) if the
    first whitespace-separated token matches a real join_code, else
    (None, body) unchanged -- so a normal offer with no prefix, or a false
    lead token that isn't anyone's actual code, parses exactly as it did
    before this existed. Only ever strips the prefix when it's a real
    match; never guesses."""
    if not body:
        return None, body
    parts = body.strip().split(None, 1)
    if not parts:
        return None, body
    candidate = parts[0].strip(".,:;-").upper()
    brokerage = get_brokerage_by_code(candidate)
    if not brokerage:
        return None, body
    remainder = parts[1] if len(parts) > 1 else ""
    return brokerage, remainder


def list_brokerage_agents(brokerage_id: int) -> list:
    """Roster + lifetime SMS-drafted-offer count per agent, most active
    first -- the concrete "your agents' offers, in one place" view a
    managing broker is actually paying to see."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    rows = cursor.execute("""
        SELECT u.phone, u.plan, u.created_at,
               (SELECT COUNT(*) FROM offers o WHERE o.phone = u.phone) AS offer_count,
               (SELECT MAX(o.created_at) FROM offers o WHERE o.phone = u.phone) AS last_offer_at
        FROM users u
        WHERE u.brokerage_id = ?
        ORDER BY offer_count DESC, u.created_at ASC
    """, (brokerage_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_brokerages_table()
