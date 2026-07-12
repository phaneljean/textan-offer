"""
agent_profiles.py — Store agent-specific defaults (name, license, title company, etc)
so their info auto-fills every offer. Uses SQLite for persistence.
"""
import sqlite3
import os

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

DEFAULTS = {
    "name": "",
    "license": "",
    "phone": "",
    "email": "",
    "brokerage": "",
    "title_company": "",
    "default_earnest_pct": 0.01,
    "default_option_fee": 250,
}

DEMO_PROFILE = {
    "name": "Demo Agent",
    "license": "TX-123456",
    "phone": "(512) 555-0100",
    "email": "agent@example.com",
    "brokerage": "Example Realty",
    "title_company": "Texas Title Co.",
    "default_earnest_pct": 0.01,
    "default_option_fee": 250,
}


def _init_profiles_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_profiles (
            source_id TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            license TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            brokerage TEXT DEFAULT '',
            title_company TEXT DEFAULT '',
            default_earnest_pct REAL DEFAULT 0.01,
            default_option_fee INTEGER DEFAULT 250
        )
    """)
    conn.commit()
    conn.close()

_init_profiles_table()


def get_agent_profile(source_id: str) -> dict:
    if source_id == "demo-web":
        return DEMO_PROFILE

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM agent_profiles WHERE source_id = ?", (source_id,)
    ).fetchone()
    conn.close()

    if row:
        return {
            "name": row["name"],
            "license": row["license"],
            "phone": row["phone"],
            "email": row["email"],
            "brokerage": row["brokerage"],
            "title_company": row["title_company"],
            "default_earnest_pct": row["default_earnest_pct"],
            "default_option_fee": row["default_option_fee"],
        }

    return {
        **DEFAULTS,
        "phone": source_id if source_id.startswith("+") else "",
    }


def save_agent_profile(source_id: str, profile: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO agent_profiles (source_id, name, license, phone, email, brokerage, title_company, default_earnest_pct, default_option_fee)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            name=excluded.name, license=excluded.license, phone=excluded.phone,
            email=excluded.email, brokerage=excluded.brokerage, title_company=excluded.title_company,
            default_earnest_pct=excluded.default_earnest_pct, default_option_fee=excluded.default_option_fee
    """, (
        source_id,
        profile.get("name", ""),
        profile.get("license", ""),
        profile.get("phone", ""),
        profile.get("email", ""),
        profile.get("brokerage", ""),
        profile.get("title_company", ""),
        profile.get("default_earnest_pct", 0.01),
        profile.get("default_option_fee", 250),
    ))
    conn.commit()
    conn.close()
