"""
crm.py -- Lightweight sales CRM for TxtAnOffer, built around one goal: more
sign-ups (2026-10-07).

Leads live in crm_leads (+ a touch log in crm_touches). What makes it more
than a spreadsheet is that each lead is joined, at render time, to what the
product already records: TC Check runs from their email, visits on their
per-lead ?src= tag, an /archive early-access sign-up, a brokerage account.
Those signals push a lead's stage forward automatically and rank the
"Do these today" list, so the leads closest to signing up float to the top.

No lead data is committed to the repo (it's public) -- leads are added on
/admin/crm (form or CSV paste) and stored only in the production database.
"""
import csv
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

STAGES = ["new", "contacted", "replied", "engaged", "trial", "signed_up", "paying", "lost"]
STAGE_LABELS = {
    "new": "New", "contacted": "Contacted", "replied": "Replied", "engaged": "Clicked / visited",
    "trial": "Tried it", "signed_up": "Signed up", "paying": "Paying", "lost": "Not a fit",
}
TOUCH_KINDS = ["email", "call", "voicemail", "reply", "meeting", "note"]


def init_crm_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT DEFAULT '', firm TEXT DEFAULT '', email TEXT DEFAULT '', phone TEXT DEFAULT '',
            market TEXT DEFAULT '', role TEXT DEFAULT '', source TEXT DEFAULT '', src_tag TEXT DEFAULT '',
            stage TEXT DEFAULT 'new', next_action TEXT DEFAULT '', next_due TEXT DEFAULT '',
            notes TEXT DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crm_email ON crm_leads(lower(email))")
    conn.execute("CREATE TABLE IF NOT EXISTS crm_dismissed (email TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crm_touches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL,
            kind TEXT NOT NULL, note TEXT DEFAULT '', created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _now():
    return datetime.utcnow().isoformat()


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def add_lead(**f) -> int:
    """Insert a lead; if one with the same email already exists, fill in its
    blank fields instead of duplicating it. Returns the lead id."""
    email = (f.get("email") or "").strip().lower()
    conn = _conn()
    if email:
        row = conn.execute("SELECT * FROM crm_leads WHERE lower(email) = ?", (email,)).fetchone()
        if row:
            sets, vals = [], []
            for k in ("name", "firm", "phone", "market", "role", "source", "src_tag", "next_action", "next_due", "notes"):
                if f.get(k) and not row[k]:
                    sets.append(f"{k} = ?")
                    vals.append(str(f[k])[:500])
            if sets:
                conn.execute(f"UPDATE crm_leads SET {', '.join(sets)}, updated_at = ? WHERE id = ?", vals + [_now(), row["id"]])
                conn.commit()
            conn.close()
            return row["id"]
    stage = f.get("stage") if f.get("stage") in STAGES else "new"
    cur = conn.execute("""
        INSERT INTO crm_leads (name, firm, email, phone, market, role, source, src_tag, stage, next_action, next_due, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, tuple(str(f.get(k) or "")[:500] for k in ("name", "firm")) + (email,) +
         tuple(str(f.get(k) or "")[:500] for k in ("phone", "market", "role", "source", "src_tag")) +
         (stage, str(f.get("next_action") or "")[:300], str(f.get("next_due") or "")[:10], str(f.get("notes") or "")[:2000], _now(), _now()))
    conn.commit()
    lid = cur.lastrowid
    conn.close()
    return lid


def import_csv(text: str) -> dict:
    """Header row required. Known columns: name, firm, email, phone, market,
    role, source, src_tag, stage, next_action, next_due, notes."""
    added = merged = skipped = 0
    reader = csv.DictReader(io.StringIO(text.strip()))
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if not (row.get("email") or row.get("name") or row.get("phone")):
            skipped += 1
            continue
        before = _count()
        add_lead(**row)
        if _count() > before:
            added += 1
        else:
            merged += 1
    return {"added": added, "merged": merged, "skipped": skipped}


def _count():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
    conn.close()
    return n


def update_lead(lead_id: int, **f):
    allowed = ("name", "firm", "email", "phone", "market", "role", "source", "src_tag", "stage", "next_action", "next_due", "notes")
    sets, vals = [], []
    for k in allowed:
        if k in f and f[k] is not None:
            v = str(f[k])[:2000]
            if k == "stage" and v not in STAGES:
                continue
            if k == "email":
                v = v.strip().lower()
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(f"UPDATE crm_leads SET {', '.join(sets)}, updated_at = ? WHERE id = ?", vals + [_now(), lead_id])
    conn.commit()
    conn.close()


def log_touch(lead_id: int, kind: str, note: str = ""):
    kind = kind if kind in TOUCH_KINDS else "note"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO crm_touches (lead_id, kind, note, created_at) VALUES (?,?,?,?)",
                 (lead_id, kind, (note or "")[:1000], _now()))
    # A logged outreach/reply moves the manual stage forward, never back.
    bump = {"email": "contacted", "call": "contacted", "voicemail": "contacted", "reply": "replied", "meeting": "replied"}.get(kind)
    if bump:
        row = conn.execute("SELECT stage FROM crm_leads WHERE id = ?", (lead_id,)).fetchone()
        if row and row[0] != "lost" and STAGES.index(bump) > STAGES.index(row[0] if row[0] in STAGES else "new"):
            conn.execute("UPDATE crm_leads SET stage = ?, updated_at = ? WHERE id = ?", (bump, _now(), lead_id))
    conn.execute("UPDATE crm_leads SET updated_at = ? WHERE id = ?", (_now(), lead_id))
    conn.commit()
    conn.close()


def dismiss_inbound(email: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO crm_dismissed (email, created_at) VALUES (?, ?)", ((email or "").strip().lower(), _now()))
    conn.commit()
    conn.close()


def delete_lead(lead_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM crm_touches WHERE lead_id = ?", (lead_id,))
    conn.execute("DELETE FROM crm_leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()


def _signals() -> dict:
    """Everything the product knows, keyed for joining to leads:
    by_email[email] and by_tag[src_tag]. Read once per page render."""
    conn = _conn()
    by_email, by_tag = {}, {}

    def em(e):
        return by_email.setdefault((e or "").strip().lower(), {"checks": 0, "last_check": "", "archive_list": False,
                                                                "brokerage": False, "paying": False, "email_capture": False})

    try:
        rows = conn.execute("""SELECT event_type, phone, metadata, created_at FROM events
                               WHERE event_type IN ('tc_check','archive_early_access','tc_check_email_captured','page_view','landing_visit')""").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        m = json.loads(r["metadata"] or "{}") if r["metadata"] else {}
        et = r["event_type"]
        if et == "tc_check" and m.get("sender"):
            s = em(m["sender"])
            s["checks"] += 1
            s["last_check"] = max(s["last_check"], r["created_at"])
        elif et == "archive_early_access" and m.get("email"):
            em(m["email"])["archive_list"] = True
        elif et == "tc_check_email_captured" and r["phone"] and "@" in (r["phone"] or ""):
            em(r["phone"])["email_capture"] = True
        elif et in ("page_view", "landing_visit"):
            tag = (m.get("source") or "").strip()
            if tag and tag != "direct":
                t = by_tag.setdefault(tag, {"visits": 0, "last_visit": ""})
                t["visits"] += 1
                t["last_visit"] = max(t["last_visit"], r["created_at"])
    try:
        for b in conn.execute("SELECT tc_email, login_emails FROM brokerages").fetchall():
            for e in [b["tc_email"]] + (b["login_emails"] or "").split(","):
                if e and "@" in e:
                    em(e)["brokerage"] = True
    except sqlite3.OperationalError:
        pass
    try:
        for p in conn.execute("""SELECT p.email, u.is_subscribed FROM agent_profiles p
                                 JOIN users u ON u.phone = p.source_id WHERE p.email != ''""").fetchall():
            s = em(p["email"])
            s["signed_up"] = True
            s["paying"] = s["paying"] or bool(p["is_subscribed"])
    except sqlite3.OperationalError:
        pass
    conn.close()
    return {"by_email": by_email, "by_tag": by_tag}


def _effective(lead: dict, sig: dict) -> dict:
    """Signal-derived stage (never moves a lead backward from its manual
    stage, never overrides 'lost') + a priority score for the today list."""
    e = sig["by_email"].get((lead.get("email") or "").lower(), {})
    t = sig["by_tag"].get(lead.get("src_tag") or "", {}) if lead.get("src_tag") else {}
    derived = "new"
    if t.get("visits"):
        derived = "engaged"
    if e.get("checks") or e.get("archive_list") or e.get("email_capture"):
        derived = "trial"
    if e.get("signed_up") or e.get("brokerage"):
        derived = "signed_up"
    if e.get("paying"):
        derived = "paying"
    manual = lead.get("stage") if lead.get("stage") in STAGES else "new"
    stage = manual if manual == "lost" else STAGES[max(STAGES.index(manual), STAGES.index(derived))]

    reasons, score = [], 0
    if e.get("checks"):
        reasons.append(f"checked {e['checks']} file{'s' if e['checks'] != 1 else ''}")
        score += 50
    if e.get("archive_list"):
        reasons.append("joined archive list")
        score += 45
    if e.get("email_capture"):
        reasons.append("gave email on TC Check")
        score += 30
    if t.get("visits"):
        reasons.append(f"{t['visits']} visit{'s' if t['visits'] != 1 else ''} on their link")
        score += 25 + min(t["visits"], 5) * 3
    if manual == "replied":
        reasons.append("replied")
        score += 40
    today = datetime.utcnow().date().isoformat()
    if lead.get("next_due") and lead["next_due"] <= today:
        reasons.append("follow-up due" if lead["next_due"] == today else "follow-up overdue")
        score += 20
    if stage in ("signed_up", "paying", "lost"):
        score = -1
    return {"stage": stage, "score": score, "reasons": reasons,
            "last_signal": max(e.get("last_check", ""), t.get("last_visit", ""))}


def list_leads(query: str = "", stage: str = "") -> list:
    conn = _conn()
    rows = [dict(r) for r in conn.execute("SELECT * FROM crm_leads ORDER BY updated_at DESC").fetchall()]
    touches = {}
    for t in conn.execute("SELECT lead_id, kind, note, created_at FROM crm_touches ORDER BY created_at DESC").fetchall():
        touches.setdefault(t["lead_id"], []).append(dict(t))
    conn.close()
    sig = _signals()
    q = (query or "").strip().lower()
    out = []
    for r in rows:
        r.update(_effective(r, sig))
        r["touches"] = touches.get(r["id"], [])
        if q and q not in " ".join(str(r.get(k, "")) for k in ("name", "firm", "email", "market", "phone", "src_tag")).lower():
            continue
        if stage and r["stage"] != stage:
            continue
        out.append(r)
    return out


def inbound_not_in_crm() -> list:
    """People who showed real intent but aren't in the CRM yet: TC Check
    users with an email, archive early-access sign-ups."""
    sig = _signals()["by_email"]
    conn = sqlite3.connect(DB_PATH)
    known = {r[0].lower() for r in conn.execute("SELECT email FROM crm_leads WHERE email != ''").fetchall()}
    known |= {r[0].lower() for r in conn.execute("SELECT email FROM crm_dismissed").fetchall()}
    conn.close()
    out = []
    for email, s in sig.items():
        if not email or "@" not in email or email in known or email.endswith("txtanoffer.com"):
            continue
        if s.get("checks") or s.get("archive_list") or s.get("email_capture"):
            why = []
            if s.get("archive_list"):
                why.append("joined archive list")
            if s.get("checks"):
                why.append(f"checked {s['checks']} file{'s' if s['checks'] != 1 else ''}")
            if s.get("email_capture"):
                why.append("gave email on TC Check")
            out.append({"email": email, "why": ", ".join(why), "last": s.get("last_check", "")})
    return sorted(out, key=lambda x: x["last"], reverse=True)


init_crm_tables()
