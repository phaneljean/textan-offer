"""
transaction_tasks.py -- The task/checklist engine behind an accepted offer's
Transaction page (app.py's /transaction/<filename>).

Scope, deliberately: this tracks TASKS (checkboxes a TC works through), not
a second deadline engine. The only due dates ever auto-assigned here are
ones deadlines.py can actually compute from real contract data (the
Effective Date, the Paragraph 5 earnest money deadline, the option period's
own termination date, and the closing date printed on the contract itself).
Title, Financing, Inspections, and Amendments have no contract-fixed date
this app has data for -- their default tasks ship with no due date, and a
TC can set one by hand. Never fabricate a due date for those.

STAGES is the fixed order the Transaction timeline renders in. A stage's
timeline state (done/current/upcoming) is derived from its own tasks'
completion, not guessed from a calendar -- see stage_states() in app.py.
"""
import sqlite3
import os
from datetime import date, datetime

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

STAGES = [
    "Accepted", "Option Period", "Earnest Money", "Title", "Financing",
    "Inspections", "Amendments", "Final Walkthrough", "Closing",
]

# (stage, label, due_kind) -- due_kind is resolved to a real date at seed
# time by _resolve_due_date(), or left None if there's nothing to resolve.
DEFAULT_TASKS = [
    ("Accepted", "Confirm Effective Date with all parties", "effective"),
    ("Accepted", "Share the Day One summary with buyer, lender, and title company", "effective"),
    ("Option Period", "Schedule inspections within the option period", "option_end"),
    # 5B: 5:00 PM local time on the end date, never moved for a weekend or
    # holiday (deadlines.option_end_date), so the task says so.
    ("Option Period", "Confirm any objections/termination notice sent by 5:00 PM local time on the due date (Par. 5B, not extended for weekends/holidays)", "option_end"),
    ("Earnest Money", "Confirm earnest money delivered to title company", "earnest_deadline"),
    ("Earnest Money", "Confirm option fee delivered to seller", "earnest_deadline"),
    ("Title", "Order title commitment", None),
    ("Title", "Review title commitment for exceptions", None),
    ("Financing", "Confirm buyer's loan application submitted", None),
    ("Financing", "Track loan approval / financing contingency", None),
    ("Inspections", "Confirm inspection reports received", None),
    ("Inspections", "Confirm repair requests or amendment, if any", None),
    ("Amendments", "Send any amendments for signature", None),
    ("Final Walkthrough", "Schedule final walkthrough", "walkthrough"),
    ("Closing", "Confirm closing documents are ready", "closing"),
    ("Closing", "Verify all required signatures are complete", "closing"),
    ("Closing", "Confirm closing date/time/location with title company", "closing"),
]


# Colorado CBS1 deals (filenames start "CBS1_", see co_filler.py). There is
# no option period or option fee in Colorado; every dated task below comes
# from the §3.1 deadline table printed on that contract (co_audit
# .contract_deadlines), so the checklist can't disagree with the contract.
CO_STAGES = [
    "Accepted", "Earnest Money", "Title", "Disclosures", "Financing",
    "Appraisal", "Inspections", "Amendments", "Final Walkthrough", "Closing",
]
CO_FIXED_TASKS = [
    ("Accepted", "Confirm the MEC date (both parties signed) with all parties", "effective"),
    ("Accepted", "Share the §3.1 deadlines with buyer, lender, and title company", "effective"),
    ("Earnest Money", "Confirm earnest money delivered to the Earnest Money Holder (§4.3)", "effective"),
    ("Amendments", "Send any Agreement to Amend/Extend for signature", None),
    ("Final Walkthrough", "Schedule final walkthrough", "walkthrough"),
    ("Closing", "Confirm closing documents are ready", "closing"),
    ("Closing", "Verify all required signatures are complete", "closing"),
    ("Closing", "Confirm closing date/time/location with the closing company", "closing"),
]


def is_co(filename: str) -> bool:
    return (filename or "").startswith("CBS1_")


def stages_for(filename: str) -> list:
    return CO_STAGES if is_co(filename) else STAGES


def _co_tasks(pdf_path: str, effective_date: date, close_date):
    """(stage, label, due) rows for a Colorado deal, in CO_STAGES order."""
    from datetime import timedelta
    import co_audit
    values = co_audit.read_contract(pdf_path) if pdf_path else None
    deadlines = co_audit.contract_deadlines(values) if values else []
    if values:
        close_date = co_audit.contract_key_dates(values)["closing"] or close_date
    rows = []
    for stage, label, due_kind in CO_FIXED_TASKS:
        due = {"effective": effective_date, "closing": close_date,
               "walkthrough": (close_date - timedelta(days=1)) if close_date else None}.get(due_kind)
        rows.append((stage, label, due))
    for d in deadlines:
        label = f"{d['label']}: {d['action']}"
        if not d["date"]:
            label += f" (contract says \"{d['raw']}\")"
        rows.append((d["stage"], label, d["date"]))
    order = {s: i for i, s in enumerate(CO_STAGES)}
    rows.sort(key=lambda r: (order[r[0]], r[2] or date.max))
    return rows


def init_transaction_tasks_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transaction_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            stage TEXT NOT NULL,
            label TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            due_date TEXT DEFAULT '',
            done_at TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tx_tasks_filename ON transaction_tasks(filename)")
    conn.commit()
    conn.close()


def _resolve_due_date(due_kind, effective_date: date, option_days, close_date):
    if not due_kind:
        return None
    from deadlines import earnest_money_deadline, option_end_date
    if due_kind == "effective":
        return effective_date
    if due_kind == "option_end":
        return option_end_date(effective_date, option_days)
    if due_kind == "earnest_deadline":
        return earnest_money_deadline(effective_date)
    if due_kind == "walkthrough":
        from datetime import timedelta
        return (close_date - timedelta(days=1)) if close_date else None
    if due_kind == "closing":
        return close_date
    return None


def seed_default_tasks(filename: str, effective_date: date, option_days, close_date, pdf_path: str = None):
    """Idempotent -- a no-op if this offer already has tasks (e.g. a
    duplicate accept POST, or a re-visit). Colorado deals (is_co) are seeded
    from their own contract's §3.1 dates via pdf_path instead of the Texas
    DEFAULT_TASKS."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    existing = cursor.execute("SELECT COUNT(*) FROM transaction_tasks WHERE filename=?", (filename,)).fetchone()[0]
    if existing:
        conn.close()
        return
    now = datetime.utcnow().isoformat()
    if is_co(filename):
        rows = _co_tasks(pdf_path, effective_date, close_date)
    else:
        rows = [(stage, label, _resolve_due_date(due_kind, effective_date, option_days, close_date))
                for stage, label, due_kind in DEFAULT_TASKS]
    for i, (stage, label, due) in enumerate(rows):
        cursor.execute("""
            INSERT INTO transaction_tasks (filename, stage, label, due_date, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (filename, stage, label, due.isoformat() if due else "", i, now))
    conn.commit()
    conn.close()


def get_tasks(filename: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, filename, stage, label, done, due_date, done_at, sort_order
        FROM transaction_tasks WHERE filename=? ORDER BY sort_order, id
    """, (filename,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def toggle_task(filename: str, task_id: int, done: bool) -> bool:
    """Scoped to filename too, not just task_id -- so a task id from one
    offer's page can never toggle another offer's task."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat() if done else ""
    cursor.execute("""
        UPDATE transaction_tasks SET done=?, done_at=? WHERE id=? AND filename=?
    """, (1 if done else 0, now, task_id, filename))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def add_task(filename: str, stage: str, label: str, due_date: str = "") -> int:
    if stage not in stages_for(filename):
        stage = stages_for(filename)[0]
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow().isoformat()
    max_order = cursor.execute("SELECT COALESCE(MAX(sort_order), 0) FROM transaction_tasks WHERE filename=?", (filename,)).fetchone()[0]
    cursor.execute("""
        INSERT INTO transaction_tasks (filename, stage, label, due_date, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (filename, stage, label.strip()[:200], due_date, max_order + 1, now))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id


def delete_task(filename: str, task_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transaction_tasks WHERE id=? AND filename=?", (task_id, filename))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


init_transaction_tasks_table()
