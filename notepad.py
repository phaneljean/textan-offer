"""
notepad.py — Deal Notepad data model: transactions, versioned raw notes,
deal sheets, and the enhancement audit trail.

The Deal Sheet is the central transaction record: drafts, reminders, and
TC Checks (later phases) all reference the transaction row created here --
a transaction exists BEFORE any draft, unlike `offers`, which only exists
once a PDF has been generated.

Doctrine, binding on everything in this module:

* Notes are append-only and verbatim. Editing the notepad inserts a NEW
  `notes` row; the latest row is the current note. Nothing ever rewrites
  or "cleans up" a stored note -- the raw note is the source of truth and
  is always shown next to whatever an enhancement proposed from it.
* Confirm-first. Every field on a deal sheet carries a state:
  "found" (user-accepted or user-typed), "confirm" (proposed, needs the
  user's eye), or absent (= missing, "Not in your notes"). Enhancements
  are proposals: they never overwrite a user-entered value, and a
  "confirm" proposal never overwrites a "found" one. Only firm ("found")
  fields serialize into a draft (see sheet_to_parsed).
* Deletes are soft. `deleted_at` hides a transaction immediately; a
  purge sweep hard-deletes its rows 30 days later (purge_expired_deleted,
  mirroring the free-PDF 30-day doctrine).

Storage follows the codebase pattern (subscriptions.py, offers_db.py,
transaction_tasks.py): one SQLite DB at DATABASE_PATH, a fresh connection
per call, tables created at import with CREATE TABLE IF NOT EXISTS.
Every helper is scoped by owner phone -- a transaction id alone never
grants access to another agent's deal.
"""
import json
import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DATABASE_PATH", "subscriptions.db")

# ---------------------------------------------------------------------------
# Canonical Deal Sheet fields (spec section 2.3). Keys align with
# parser.py's parsed dict, tc_audit.CHECKED_FIELDS, and agent_profiles so a
# sheet can serialize into a draft and be cross-read against a contract
# check. `kind` drives validation and display formatting.
# ---------------------------------------------------------------------------
FIELD_DEFS = [
    # key, label, kind
    ("address", "Address", "string"),
    ("city", "City", "string"),
    ("county", "County", "string"),
    ("buyer_name", "Buyer", "party"),
    ("seller_name", "Seller", "party"),
    ("price", "Sales price", "money"),
    ("down_payment_pct", "Down payment %", "pct"),
    ("down_payment_amount", "Down payment $", "money"),
    ("financing_type", "Financing", "string"),
    ("earnest_money_amount", "Earnest money", "money"),
    ("earnest_pct", "Earnest %", "pct"),
    ("option_fee_amount", "Option fee", "money"),
    ("option_days", "Option days", "int"),
    ("escrow_agent_name", "Escrow agent", "party"),
    ("title_company", "Title company", "party"),
    ("effective_date", "Effective date", "date"),
    ("close_days", "Close in (days)", "int"),
    ("closing_date", "Closing date", "date"),
    ("hoa", "HOA", "text"),
    ("contingencies", "Contingencies", "text"),
    ("inclusions", "Inclusions", "text"),
    ("seller_concessions", "Seller concessions", "text"),
    ("special_notes", "Special notes", "text"),
]
FIELD_KEYS = [k for k, _, _ in FIELD_DEFS]
FIELD_LABELS = {k: label for k, label, _ in FIELD_DEFS}
FIELD_KINDS = {k: kind for k, _, kind in FIELD_DEFS}

MONEY_FIELDS = {k for k, kind in FIELD_KINDS.items() if kind == "money"}
# Fields whose proposal is dropped outright when its source span cannot be
# verified against the raw note (validation lives in note_enhancer.py; the
# sets live here so both modules agree on the field vocabulary).
STRICT_SPAN_FIELDS = {k for k, kind in FIELD_KINDS.items()
                      if kind in ("money", "pct", "int", "date", "party", "string")}
FREE_TEXT_FIELDS = {k for k, kind in FIELD_KINDS.items() if kind == "text"}
TX_ONLY_FIELDS = {"option_fee_amount", "option_days"}
FIELD_STATES = ("found", "confirm")

PURGE_AFTER_DAYS = 30


def _now():
    return datetime.utcnow().isoformat()


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_notepad_tables():
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            brokerage_id INTEGER,
            offer_filename TEXT DEFAULT '',
            state TEXT DEFAULT 'TX',
            status TEXT DEFAULT 'notepad',
            title TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_transactions_phone
        ON transactions(phone, updated_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            source TEXT DEFAULT 'web',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_notes_tx ON notes(transaction_id, created_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_sheets (
            transaction_id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL,
            summary TEXT DEFAULT '',
            fields_json TEXT DEFAULT '{}',
            enhanced_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_enhancements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            note_id INTEGER NOT NULL,
            phone TEXT NOT NULL,
            provider TEXT DEFAULT '',
            model TEXT DEFAULT '',
            prompt_version TEXT DEFAULT '',
            raw_output TEXT DEFAULT '',
            applied_json TEXT DEFAULT '',
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_notepad_tables()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def create_transaction(phone: str, state: str = "TX", title: str = "",
                       brokerage_id=None) -> dict:
    """Create a transaction plus its empty deal sheet. Returns the
    transaction dict."""
    now = _now()
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO transactions (phone, brokerage_id, state, status, title,
                                  created_at, updated_at)
        VALUES (?, ?, ?, 'notepad', ?, ?, ?)
    """, (phone, brokerage_id, state if state in ("TX", "CO") else "TX",
          title or "", now, now))
    tx_id = cur.lastrowid
    conn.execute("""
        INSERT INTO deal_sheets (transaction_id, phone, created_at, updated_at)
        VALUES (?, ?, ?, ?)
    """, (tx_id, phone, now, now))
    conn.commit()
    conn.close()
    return get_transaction(tx_id, phone)


def get_transaction(tx_id, phone: str, include_deleted: bool = False):
    """One transaction owned by `phone`, or None. A transaction id that
    belongs to another phone is indistinguishable from a missing one --
    that is the 404 rule every route relies on."""
    try:
        tx_id = int(tx_id)
    except (TypeError, ValueError):
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM transactions WHERE id = ? AND phone = ?", (tx_id, phone)
    ).fetchone()
    conn.close()
    if not row:
        return None
    tx = dict(row)
    if tx.get("deleted_at") and not include_deleted:
        return None
    return tx


def list_transactions(phone: str) -> list:
    """Non-deleted transactions for the dashboard/list page, most recently
    updated first, each with its confirm count attached."""
    conn = _conn()
    rows = conn.execute("""
        SELECT t.*, COALESCE(ds.fields_json, '{}') AS fields_json
        FROM transactions t
        LEFT JOIN deal_sheets ds ON ds.transaction_id = t.id
        WHERE t.phone = ? AND (t.deleted_at IS NULL OR t.deleted_at = '')
        ORDER BY t.updated_at DESC
    """, (phone,)).fetchall()
    conn.close()
    out = []
    for row in rows:
        tx = dict(row)
        fields = _parse_fields(tx.pop("fields_json", "{}"))
        tx["confirm_count"] = confirm_count(fields)
        tx["found_count"] = sum(1 for f in fields.values()
                                if f.get("state") == "found")
        out.append(tx)
    return out


_TX_WRITABLE = ("title", "state", "status", "offer_filename", "brokerage_id")


def update_transaction(tx_id, phone: str, **kwargs) -> bool:
    tx = get_transaction(tx_id, phone)
    if not tx:
        return False
    sets = {k: v for k, v in kwargs.items() if k in _TX_WRITABLE}
    if not sets:
        return True
    if "state" in sets and sets["state"] not in ("TX", "CO"):
        sets["state"] = "TX"
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn = _conn()
    conn.execute(
        f"UPDATE transactions SET {assignments}, updated_at = ? WHERE id = ? AND phone = ?",
        (*sets.values(), _now(), tx["id"], phone),
    )
    conn.commit()
    conn.close()
    return True


def soft_delete_transaction(tx_id, phone: str) -> bool:
    tx = get_transaction(tx_id, phone)
    if not tx:
        return False
    conn = _conn()
    conn.execute(
        "UPDATE transactions SET deleted_at = ?, updated_at = ? WHERE id = ? AND phone = ?",
        (_now(), _now(), tx["id"], phone),
    )
    conn.commit()
    conn.close()
    return True


def purge_expired_deleted(retention_days: int = PURGE_AFTER_DAYS) -> int:
    """Hard-delete transactions soft-deleted more than `retention_days`
    ago, with all of their notepad rows. Returns how many transactions
    were purged. (Wiring into a piggyback sweep is a launch step; the
    helper is here so the retention rule exists in exactly one place.)"""
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    conn = _conn()
    rows = conn.execute(
        "SELECT id FROM transactions WHERE deleted_at != '' AND deleted_at < ?",
        (cutoff,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    for tx_id in ids:
        conn.execute("DELETE FROM notes WHERE transaction_id = ?", (tx_id,))
        conn.execute("DELETE FROM note_enhancements WHERE transaction_id = ?", (tx_id,))
        conn.execute("DELETE FROM deal_sheets WHERE transaction_id = ?", (tx_id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return len(ids)


# ---------------------------------------------------------------------------
# Notes -- append-only, verbatim
# ---------------------------------------------------------------------------

def add_note(tx_id, phone: str, raw_text: str, source: str = "web"):
    """Append a new note version. Returns the note dict, or None when the
    transaction isn't owned by `phone`. The text is stored exactly as
    given -- never stripped, normalized, or cleaned."""
    tx = get_transaction(tx_id, phone)
    if not tx:
        return None
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO notes (transaction_id, phone, raw_text, source, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (tx["id"], phone, raw_text, source or "web", _now()))
    note_id = cur.lastrowid
    conn.execute(
        "UPDATE transactions SET updated_at = ? WHERE id = ?", (_now(), tx["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_latest_note(tx_id):
    """The current note for a transaction (ownership is the caller's
    check -- routes load the transaction by phone first)."""
    conn = _conn()
    row = conn.execute("""
        SELECT * FROM notes WHERE transaction_id = ?
        ORDER BY id DESC LIMIT 1
    """, (tx_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_notes(tx_id) -> list:
    """Every note version, newest first -- the verbatim history."""
    conn = _conn()
    rows = conn.execute("""
        SELECT * FROM notes WHERE transaction_id = ? ORDER BY id DESC
    """, (tx_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Deal sheets
# ---------------------------------------------------------------------------

def _parse_fields(fields_json) -> dict:
    try:
        fields = json.loads(fields_json or "{}")
    except (TypeError, ValueError):
        return {}
    return fields if isinstance(fields, dict) else {}


def get_deal_sheet(tx_id, phone: str):
    """The deal sheet for an owned transaction, or None. Self-heals the
    1:1 row if a transaction somehow exists without one."""
    tx = get_transaction(tx_id, phone)
    if not tx:
        return None
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM deal_sheets WHERE transaction_id = ?", (tx["id"],)
    ).fetchone()
    if not row:
        now = _now()
        conn.execute("""
            INSERT INTO deal_sheets (transaction_id, phone, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (tx["id"], phone, now, now))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM deal_sheets WHERE transaction_id = ?", (tx["id"],)
        ).fetchone()
    conn.close()
    sheet = dict(row)
    sheet["fields"] = _parse_fields(sheet.pop("fields_json", "{}"))
    return sheet


def _write_sheet(tx_id, phone, fields, summary=None, enhanced_at=None):
    now = _now()
    conn = _conn()
    sets = ["fields_json = ?", "updated_at = ?"]
    params = [json.dumps(fields), now]
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
    if enhanced_at is not None:
        sets.append("enhanced_at = ?")
        params.append(enhanced_at)
    conn.execute(
        f"UPDATE deal_sheets SET {', '.join(sets)} WHERE transaction_id = ? AND phone = ?",
        (*params, tx_id, phone),
    )
    conn.execute(
        "UPDATE transactions SET updated_at = ? WHERE id = ? AND phone = ?",
        (now, tx_id, phone),
    )
    conn.commit()
    conn.close()


def _title_from_address(tx_id, phone, fields):
    """A deal with no title yet borrows its address as the display label."""
    tx = get_transaction(tx_id, phone)
    if tx and not tx.get("title"):
        addr = (fields.get("address") or {}).get("value")
        if addr:
            update_transaction(tx_id, phone, title=str(addr))


def set_field(tx_id, phone: str, key: str, value, state: str = "found",
              origin: str = "user", span: str = ""):
    """Set one field (the fields endpoint: user accept/edit), or clear it
    back to missing with value=None. Returns the full fields dict after
    the write, or None when the transaction isn't owned / key unknown."""
    if key not in FIELD_KEYS:
        return None
    sheet = get_deal_sheet(tx_id, phone)
    if sheet is None:
        return None
    fields = dict(sheet["fields"])
    if value is None:
        fields.pop(key, None)
    else:
        if state not in FIELD_STATES:
            state = "found"
        fields[key] = {
            "value": value,
            "state": state,
            "span": span or "",
            "origin": origin,
            "updated_at": _now(),
        }
    _write_sheet(sheet["transaction_id"], phone, fields)
    _title_from_address(sheet["transaction_id"], phone, fields)
    return fields


def set_summary(tx_id, phone: str, summary: str):
    sheet = get_deal_sheet(tx_id, phone)
    if sheet is None:
        return None
    _write_sheet(sheet["transaction_id"], phone, sheet["fields"], summary=summary)
    return get_deal_sheet(tx_id, phone)


def confirm_count(fields: dict) -> int:
    return sum(1 for f in (fields or {}).values() if f.get("state") == "confirm")


# ---------------------------------------------------------------------------
# Enhancement merge + audit
# ---------------------------------------------------------------------------

def _profile_default_fields(tx: dict, fields: dict, profile: dict) -> dict:
    """Local profile-default proposals for slots the enhancement left
    missing (spec 4.1: applied locally, never sent to any model, always
    Confirm). Returns {key: field} to add."""
    if not profile:
        return {}
    out = {}
    if "earnest_pct" not in fields and profile.get("default_earnest_pct"):
        out["earnest_pct"] = {
            "value": profile["default_earnest_pct"], "state": "confirm",
            "span": "", "origin": "profile_default",
            "note": "Your saved default — confirm it applies to this deal.",
            "updated_at": _now(),
        }
    if tx.get("state") == "TX" and "option_fee_amount" not in fields \
            and profile.get("default_option_fee"):
        out["option_fee_amount"] = {
            "value": profile["default_option_fee"], "state": "confirm",
            "span": "", "origin": "profile_default",
            "note": "Your saved default — confirm it applies to this deal.",
            "updated_at": _now(),
        }
    if "title_company" not in fields and (profile.get("title_company") or "").strip():
        out["title_company"] = {
            "value": profile["title_company"].strip(), "state": "confirm",
            "span": "", "origin": "profile_default",
            "note": "Your saved title company — confirm for this deal.",
            "updated_at": _now(),
        }
    return out


def apply_enhancement(tx_id, phone: str, note: dict, enhancer_result: dict,
                      validated_fields: dict, validation_warnings=(),
                      profile: dict = None) -> dict:
    """Merge validated enhancement proposals into the deal sheet under
    the confirm-first merge rules, fill gaps with profile defaults,
    and write the immutable audit row. Returns
    {"applied": {...}, "warnings": [...], "fields": <full sheet fields>}.

    Merge rules (spec 4.4.5):
    * a field the user entered (origin == "user") is NEVER overwritten --
      a differing proposal becomes a warning instead;
    * a "confirm" proposal never overwrites a "found" value;
    * a "found" proposal may overwrite a "confirm"/missing slot;
    * re-running enhance is always safe.
    """
    sheet = get_deal_sheet(tx_id, phone)
    if sheet is None:
        return {"applied": {}, "warnings": ["Transaction not found."], "fields": {}}
    tx = get_transaction(tx_id, phone)
    fields = dict(sheet["fields"])
    applied = {}
    warnings = list(validation_warnings or ())

    default_origin = {"regex": "regex", "anthropic": "llm"}.get(
        enhancer_result.get("provider", ""), "regex")
    for key, proposal in (validated_fields or {}).items():
        if key not in FIELD_KEYS:
            continue
        existing = fields.get(key)
        if existing and existing.get("origin") == "user":
            if existing.get("value") != proposal.get("value"):
                warnings.append(
                    f"Your notes now suggest {FIELD_LABELS.get(key, key)} is "
                    f"“{proposal.get('value')}”, but the sheet keeps the value "
                    f"you entered (“{existing.get('value')}”).")
            continue
        if existing and existing.get("state") == "found" \
                and proposal.get("state") != "found":
            continue
        merged = {
            "value": proposal.get("value"),
            "state": proposal.get("state") if proposal.get("state") in FIELD_STATES else "confirm",
            "span": proposal.get("span", ""),
            "origin": proposal.get("origin") or default_origin,
            "updated_at": _now(),
        }
        if proposal.get("note"):
            merged["note"] = proposal["note"]
        fields[key] = merged
        applied[key] = merged

    for key, field in _profile_default_fields(tx, fields, profile or {}).items():
        fields[key] = field
        applied[key] = field

    summary = enhancer_result.get("summary") or None
    _write_sheet(tx["id"], phone, fields,
                 summary=summary if summary else None,
                 enhanced_at=_now())
    _title_from_address(tx["id"], phone, fields)

    conn = _conn()
    conn.execute("""
        INSERT INTO note_enhancements (transaction_id, note_id, phone, provider,
            model, prompt_version, raw_output, applied_json, input_tokens,
            output_tokens, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        tx["id"], (note or {}).get("id") or 0, phone,
        enhancer_result.get("provider", ""), enhancer_result.get("model", ""),
        enhancer_result.get("prompt_version", ""),
        json.dumps({"summary": enhancer_result.get("summary", ""),
                    "fields": enhancer_result.get("fields", {})}),
        json.dumps(applied),
        int(enhancer_result.get("input_tokens") or 0),
        int(enhancer_result.get("output_tokens") or 0),
        enhancer_result.get("status", "ok"), _now(),
    ))
    conn.commit()
    conn.close()
    return {"applied": applied, "warnings": warnings, "fields": fields}


def list_enhancements(tx_id, phone: str) -> list:
    tx = get_transaction(tx_id, phone)
    if not tx:
        return []
    conn = _conn()
    rows = conn.execute("""
        SELECT * FROM note_enhancements WHERE transaction_id = ?
        ORDER BY id DESC
    """, (tx["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Draft serialization (spec 2.3): only firm fields become a parsed dict.
# ---------------------------------------------------------------------------

def sheet_to_parsed(fields: dict) -> dict:
    """Serialize a deal sheet's fields into parser.py's `parsed` shape.
    Only state == "found" fields are included -- a Confirm value never
    reaches a contract by accident."""
    firm = {k: f.get("value") for k, f in (fields or {}).items()
            if f.get("state") == "found" and f.get("value") not in (None, "")}
    parsed = {}
    for src, dst in (("price", "price"),
                     ("down_payment_pct", "down_payment_pct"),
                     ("close_days", "close_days"),
                     ("address", "address"),
                     ("county", "county"),
                     ("city", "city"),
                     ("financing_type", "financing_type"),
                     ("option_days", "inspection_days")):
        if src in firm:
            parsed[dst] = firm[src]
    if firm.get("hoa"):
        parsed["has_hoa"] = True
    return parsed


def blocking_confirm_fields(fields: dict) -> list:
    """Confirm-state fields that map onto a filled contract field -- the
    list a draft action must walk the user through first (spec 2.3)."""
    contract_keys = ("address", "city", "county", "buyer_name", "seller_name",
                     "price", "down_payment_pct", "down_payment_amount",
                     "financing_type", "earnest_money_amount", "earnest_pct",
                     "option_fee_amount", "option_days", "escrow_agent_name",
                     "title_company", "effective_date", "close_days",
                     "closing_date")
    return [k for k in contract_keys
            if (fields or {}).get(k, {}).get("state") == "confirm"]


def format_field_value(key: str, value) -> str:
    """Display formatting for one field value (dashboard/report style)."""
    if value is None or value == "":
        return ""
    kind = FIELD_KINDS.get(key, "string")
    if kind == "money":
        try:
            return f"${int(round(float(value))):,}"
        except (TypeError, ValueError):
            return str(value)
    if kind == "pct":
        try:
            pct = float(value) * 100
            return f"{pct:g}%"
        except (TypeError, ValueError):
            return str(value)
    return str(value)
