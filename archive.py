"""
archive.py -- Brokerage contract archive (built 2026-10-07).

Executed contracts a brokerage forwards to tc@check.txtanoffer.com (from a
sender linked to that brokerage) or uploads on /broker/archive are checked
by TC Check and then KEPT here -- unlike the free check, which deletes the
file after the report. Files live on the Railway volume under
OUTPUT_DIR/archive/<brokerage_id>/ (a subfolder, so cleanup.py's 30-day
top-level PDF sweep never touches them) and are purged after
ARCHIVE_RETENTION_DAYS (5 years). Access is only through a signed-in broker
session (see broker_auth.py) -- never by a guessable URL.
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")
OUTPUT_DIR = os.environ.get("OFFER_OUTPUT_DIR", "generated_offers")
ARCHIVE_DIR = os.path.join(OUTPUT_DIR, "archive")
ARCHIVE_RETENTION_DAYS = int(os.environ.get("ARCHIVE_RETENTION_DAYS", 5 * 365))
MAX_FILE_BYTES = 25 * 1024 * 1024


def init_archive_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS archive_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brokerage_id INTEGER NOT NULL,
            stored_name TEXT NOT NULL,
            original_name TEXT,
            address TEXT DEFAULT '',
            city TEXT DEFAULT '',
            county TEXT DEFAULT '',
            source TEXT NOT NULL,            -- 'forward' or 'upload'
            sender TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            sha256 TEXT,
            recognized INTEGER DEFAULT 0,
            blocker_count INTEGER DEFAULT 0,
            issue_count INTEGER DEFAULT 0,
            issues_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_archive_brokerage ON archive_files(brokerage_id, created_at)")
    # Brokerage-level settings for the archive. ALTER has no IF NOT EXISTS
    # in SQLite -- a duplicate-column error just means it's already there.
    for ddl in (
        "ALTER TABLE brokerages ADD COLUMN login_emails TEXT DEFAULT ''",   # extra sign-in emails, comma-separated
        "ALTER TABLE brokerages ADD COLUMN archive_forwards INTEGER DEFAULT 1",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def _row(r):
    d = dict(r)
    try:
        d["issues"] = json.loads(d.pop("issues_json") or "[]")
    except ValueError:
        d["issues"] = []
    return d


def login_emails_for(brokerage: dict) -> set:
    emails = {(brokerage.get("tc_email") or "").strip().lower()}
    emails |= {e.strip().lower() for e in (brokerage.get("login_emails") or "").split(",")}
    return {e for e in emails if "@" in e}


def find_brokerage_for_login(email: str):
    """Brokerage whose TC/login email matches -- who may sign in."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM brokerages").fetchall()]
    conn.close()
    for b in rows:
        if email in login_emails_for(b):
            return b
    return None


def find_brokerage_for_sender(email: str):
    """Brokerage a forwarded email belongs to: its own TC/login emails, or an
    agent on its roster (agent profile email -> phone -> users.brokerage_id).
    Returns None for everyone else, whose forwards stay check-only."""
    b = find_brokerage_for_login(email)
    if b:
        return b
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT b.* FROM agent_profiles p
            JOIN users u ON u.phone = p.source_id
            JOIN brokerages b ON b.id = u.brokerage_id
            WHERE lower(p.email) = ? LIMIT 1
        """, (email,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    return dict(row) if row else None


def save_file(brokerage_id: int, data: bytes, original_name: str, source: str, sender: str = "",
              result: dict = None) -> dict:
    """Store one PDF for a brokerage. result = check_tc_file() output for
    this file (or the forward's main contract), used for the address and
    check status. Identical bytes already in this brokerage's archive are
    not stored twice -- the existing record is returned instead."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("File is larger than 25 MB.")
    digest = hashlib.sha256(data).hexdigest()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        "SELECT * FROM archive_files WHERE brokerage_id = ? AND sha256 = ?", (brokerage_id, digest)
    ).fetchone()
    if existing:
        conn.close()
        out = _row(existing)
        out["duplicate"] = True
        return out

    folder = os.path.join(ARCHIVE_DIR, str(int(brokerage_id)))
    os.makedirs(folder, exist_ok=True)
    stored = uuid.uuid4().hex + ".pdf"
    path = os.path.join(folder, stored)
    with open(path, "wb") as f:
        f.write(data)

    result = result or {}
    prop = result.get("property") or {}
    issues = result.get("issues") or []
    now = datetime.utcnow().isoformat()
    cur = conn.execute("""
        INSERT INTO archive_files (brokerage_id, stored_name, original_name, address, city, county, source,
                                   sender, size, sha256, recognized, blocker_count, issue_count, issues_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (brokerage_id, stored, (original_name or "file.pdf")[:200], prop.get("address", ""), prop.get("city", ""),
          prop.get("county", ""), source, (sender or "")[:200], len(data), digest,
          1 if result.get("recognized") else 0,
          sum(1 for i in issues if i.get("severity") == "blocker"), len(issues),
          json.dumps([{"severity": i.get("severity"), "key": i.get("key"), "message": i.get("message")} for i in issues]),
          now))
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM archive_files WHERE id = ?", (new_id,)).fetchone()
    conn.close()
    out = _row(row)
    out["duplicate"] = False
    return out


def list_files(brokerage_id: int, query: str = "", limit: int = 1000) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    q = (query or "").strip()
    rows = conn.execute("""
        SELECT * FROM archive_files WHERE brokerage_id = ?
          AND (? = '' OR address LIKE ? OR original_name LIKE ? OR city LIKE ?)
        ORDER BY created_at DESC LIMIT ?
    """, (brokerage_id, q, f"%{q}%", f"%{q}%", f"%{q}%", limit)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def get_file(file_id: int, brokerage_id: int):
    """Only ever returns a file belonging to this brokerage."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM archive_files WHERE id = ? AND brokerage_id = ?",
                       (file_id, brokerage_id)).fetchone()
    conn.close()
    if not row:
        return None
    d = _row(row)
    d["path"] = os.path.join(ARCHIVE_DIR, str(int(brokerage_id)), d["stored_name"])
    return d


def delete_file(file_id: int, brokerage_id: int) -> bool:
    f = get_file(file_id, brokerage_id)
    if not f:
        return False
    try:
        os.remove(f["path"])
    except OSError:
        pass
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM archive_files WHERE id = ? AND brokerage_id = ?", (file_id, brokerage_id))
    conn.commit()
    conn.close()
    return True


def set_archive_forwards(brokerage_id: int, on: bool):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE brokerages SET archive_forwards = ? WHERE id = ?", (1 if on else 0, brokerage_id))
    conn.commit()
    conn.close()


def purge_expired(max_age_days: int = ARCHIVE_RETENTION_DAYS) -> int:
    """Delete archived files older than the retention period. Called from
    cleanup.run_cleanup_if_due()."""
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, brokerage_id, stored_name FROM archive_files WHERE created_at < ?",
                            (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return 0
    for r in rows:
        try:
            os.remove(os.path.join(ARCHIVE_DIR, str(r["brokerage_id"]), r["stored_name"]))
        except OSError:
            pass
        conn.execute("DELETE FROM archive_files WHERE id = ?", (r["id"],))
    conn.commit()
    conn.close()
    return len(rows)


init_archive_tables()
