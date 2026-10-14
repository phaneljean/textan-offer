"""
co_deadline_tool.py -- the free, no-upload Colorado CBS1 §3.1 deadline
calculator (/tools/colorado-deadlines and the /embed/colorado-deadlines
widget, 2026-10-13).

TC Check needs a client's contract, which is a privacy ask nobody outside
has said yes to yet. This needs nothing but dates the TC already has, so it
can sit on a brokerage's or TC's own site. Nothing entered is stored: the
page sends the entries, gets the table back, and the server only logs an
anonymous "a calculation ran" event (surface + embedding host).

Rules come from the 2026 CBS1's own text, not general practice:
- §3.3.2: "In computing a period of days (e.g., three days after MEC), when
  the ending date is not specified, the first day is excluded and the last
  day is included." So "MEC + 10" is simply MEC + 10 calendar days -- the
  form says "day", never "business day".
- §3.3.3: "If any deadline falls on a Saturday, Sunday or federal or
  Colorado state holiday (Holiday), such deadline [ ] Will [ ] Will Not be
  extended to the next day that is not a Saturday, Sunday or Holiday.
  Should neither box be checked, the deadline will not be extended." The
  date only moves when the user says "Will" is checked; otherwise it is
  flagged and left where it is.
- §3.2: a blank, "N/A" or "Deleted" deadline "is not applicable and the
  corresponding provision containing the deadline is deleted."
Holidays, date parsing, N/A detection, row labels and the delivery ->
objection -> resolution chains are co_audit's, so the calculator and TC
Check can't drift apart.
"""
import hashlib
import re
from datetime import date, datetime, timedelta

import co_audit

# §3.1 rows as printed on the 2026 CBS1 (pages 3-4): (key, item no.,
# Reference column, table group, label). Keys match co_audit.DEADLINES where
# co_audit has the row. Left out: item 1 Time of Day Deadline and items 41/43
# (times, not dates), and item 42 Acceptance Deadline Date -- it comes before
# MEC by definition, so "days after MEC" can't produce it.
ROWS = [
    ("alt_earnest", 2, "§ 4", "", "Alternative Earnest Money Deadline"),
    ("record_title", 3, "§ 8", "Title", "Record Title Deadline (and Tax Certificate)"),
    ("record_title_obj", 4, "§ 8", "Title", "Record Title Objection Deadline"),
    ("off_record", 5, "§ 8", "Title", "Off-Record Title Deadline"),
    ("off_record_obj", 6, "§ 8", "Title", "Off-Record Title Objection Deadline"),
    ("title_resolution", 7, "§ 8", "Title", "Title Resolution Deadline"),
    ("third_party", 8, "§ 8", "Title", "Third Party Right to Purchase/Approve Deadline"),
    ("assoc_docs", 9, "§ 7", "Owners\u2019 Association", "Association Documents Deadline"),
    ("assoc_term", 10, "§ 7", "Owners\u2019 Association", "Association Documents Termination Deadline"),
    ("spd", 11, "§ 10", "Seller\u2019s Disclosures", "Seller\u2019s Property Disclosure Deadline"),
    ("lead_disclosure", 12, "§ 10", "Seller\u2019s Disclosures", "Lead-Based Paint Disclosure Deadline"),
    ("loan_app", 13, "§ 5", "Loan and Credit", "New Loan Application Deadline"),
    ("loan_terms", 14, "§ 5", "Loan and Credit", "New Loan Terms Deadline"),
    ("loan_avail", 15, "§ 5", "Loan and Credit", "New Loan Availability Deadline"),
    ("credit", 16, "§ 5", "Loan and Credit", "Buyer\u2019s Credit Information Deadline"),
    ("credit_disapproval", 17, "§ 5", "Loan and Credit", "Disapproval of Buyer\u2019s Credit Information Deadline"),
    ("existing_loan", 18, "§ 5", "Loan and Credit", "Existing Loan Deadline"),
    ("existing_loan_term", 19, "§ 5", "Loan and Credit", "Existing Loan Termination Deadline"),
    ("loan_transfer", 20, "§ 5", "Loan and Credit", "Loan Transfer Approval Deadline"),
    ("seller_fin", 21, "§ 4", "Loan and Credit", "Seller or Private Financing Deadline"),
    ("appraisal", 22, "§ 6", "Appraisal", "Appraisal Deadline"),
    ("appraisal_obj", 23, "§ 6", "Appraisal", "Appraisal Objection Deadline"),
    ("appraisal_res", 24, "§ 6", "Appraisal", "Appraisal Resolution Deadline"),
    ("ilc", 25, "§ 9", "Survey", "New ILC or New Survey Deadline"),
    ("ilc_obj", 26, "§ 9", "Survey", "New ILC or New Survey Objection Deadline"),
    ("ilc_res", 27, "§ 9", "Survey", "New ILC or New Survey Resolution Deadline"),
    ("water_rights", 28, "§ 2", "Inspection and Due Diligence", "Water Rights Examination Deadline"),
    ("mineral_rights", 29, "§ 8", "Inspection and Due Diligence", "Mineral Rights Examination Deadline"),
    ("insp_term", 30, "§ 10", "Inspection and Due Diligence", "Inspection Termination Deadline"),
    ("insp_obj", 31, "§ 10", "Inspection and Due Diligence", "Inspection Objection Deadline"),
    ("insp_res", 32, "§ 10", "Inspection and Due Diligence", "Inspection Resolution Deadline"),
    ("insurance_term", 33, "§ 10", "Inspection and Due Diligence", "Property Insurance Termination Deadline"),
    ("dd_delivery", 34, "§ 10", "Inspection and Due Diligence", "Due Diligence Documents Delivery Deadline"),
    ("dd_obj", 35, "§ 10", "Inspection and Due Diligence", "Due Diligence Documents Objection Deadline"),
    ("dd_res", 36, "§ 10", "Inspection and Due Diligence", "Due Diligence Documents Resolution Deadline"),
    ("conditional_sale", 37, "§ 10", "Inspection and Due Diligence", "Conditional Sale Deadline"),
    ("lead_term", 38, "§ 10", "Inspection and Due Diligence", "Lead-Based Paint Termination Deadline"),
    ("closing", 39, "§ 12", "Closing and Possession", "Closing Date"),
    ("possession", 40, "§ 17", "Closing and Possession", "Possession Date"),
]
ROW_KEYS = [r[0] for r in ROWS]
_BY_KEY = {r[0]: r for r in ROWS}
# "Closing Date" and "Possession Date" are dates, not deadlines; §3.3.3
# speaks of "any deadline", so the calculator never moves these two -- a
# weekend closing is flagged for the closing company instead (same as TC
# Check's co_closing_weekend).
_NOT_DEADLINES = {"closing", "possession"}

EXTEND_CHOICES = ("will", "will_not", "")   # "" = neither §3.3.3 box checked
MAX_DAYS = 400

_DAYS = re.compile(r"^\s*(?:mec\s*\+?\s*)?(\d{1,3})\s*(?:days?|d)?\s*(?:after\s+mec)?\s*$", re.I)

SOURCE_NOTE = ("Calculates dates from what you enter; confirm against the signed contract. "
               "Not legal advice. Not affiliated with the Colorado Real Estate Commission.")
SEC_3_2 = ("§3.2: a deadline left blank or completed with \u201cN/A\u201d or \u201cDeleted\u201d is not applicable, "
           "and the provision containing it is deleted.")
FHA_VA_NOTE = ("CBS1 note under §3.1: if the FHA or VA box is checked in §4.5.3, the Appraisal deadlines "
               "(items 22-24) do not apply.")
TIME_NOTE = ("Each deadline ends at 11:59 p.m. Mountain Time, unless the contract fills in item 1, Time of Day "
             "Deadline, which then applies to the Objection, Resolution, Examination and Termination Deadlines (§3.3.1).")


def fmt(d: date) -> str:
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


def parse_entry(text: str):
    """("deleted", None) | ("days", int) | ("date", date) | ("invalid", None)."""
    t = (text or "").strip()
    if co_audit._blank_or_na(t):
        return "deleted", None
    m = _DAYS.match(t)
    if m:
        n = int(m.group(1))
        return ("days", n) if n <= MAX_DAYS else ("invalid", None)
    d = co_audit._parse_date(t)
    return ("date", d) if d else ("invalid", None)


def _non_business_reason(d: date):
    """'a Saturday' / 'a Sunday' / 'Cabrini Day (Colorado)' / None."""
    if d.weekday() == 5:
        return "a Saturday"
    if d.weekday() == 6:
        return "a Sunday"
    return co_audit.holiday_name(d)


def _next_business_day(d: date) -> date:
    d += timedelta(days=1)
    while not co_audit.is_business_day(d):
        d += timedelta(days=1)
    return d


def _flag(level: str, text: str) -> dict:
    return {"level": level, "text": text}


def compute(mec: date, entries: dict, extend: str = "") -> dict:
    """Builds the §3.1 table. `entries` maps row key -> what the user typed
    (days after MEC, a date, N/A/Deleted or blank). `extend` is the §3.3.3
    box: "will", "will_not" or "" (neither checked = not extended)."""
    extend = extend if extend in EXTEND_CHOICES else ""
    rows = []
    for key, item, ref, group, label in ROWS:
        raw = (entries.get(key) or "").strip()[:40]
        kind, val = parse_entry(raw)
        row = {"key": key, "item": item, "ref": ref, "group": group, "label": label, "input": raw,
               "kind": kind, "date": None, "effective": None, "flags": []}
        if kind == "deleted":
            row["flags"].append(_flag("info", "Blank, N/A or Deleted. Under §3.2 this deadline is not applicable and the provision containing it is deleted."))
        elif kind == "invalid":
            row["flags"].append(_flag("warn", "Couldn\u2019t read this entry. Use a number of days after MEC, a date (MM/DD/YYYY), or N/A."))
        else:
            d = mec + timedelta(days=val) if kind == "days" else val
            row["date"], row["effective"] = d, d
            if kind == "days":
                row["days"] = val
            why = _non_business_reason(d)
            if why and key in _NOT_DEADLINES:
                if key == "closing":
                    msg = (f"Falls on {why}. Confirm the closing company is open."
                           if d.weekday() >= 5 else
                           f"Falls on {why}. Banks may not move wires on a holiday; confirm the closing company and lender can fund.")
                else:
                    msg = f"Falls on {why}. Confirm."
                row["flags"].append(_flag("warn", msg))
            elif why and extend == "will":
                nd = _next_business_day(d)
                row["effective"] = nd
                row["flags"].append(_flag("warn", f"Falls on {why}. \u00a73.3.3 \u201cWill\u201d is checked, so it is extended to "
                                                  f"{fmt(nd)}, the next day that isn\u2019t a Saturday, Sunday or Holiday. Confirm."))
            elif why:
                box = ("\u00a73.3.3 \u201cWill Not\u201d is checked" if extend == "will_not"
                       else "Neither \u00a73.3.3 box is checked")
                row["flags"].append(_flag("warn", f"Falls on {why}. {box}, so the deadline is not extended. Confirm."))
            if d < mec:
                row["flags"].append(_flag("warn", f"Before MEC ({fmt(mec)})."))
        rows.append(row)

    by_key = {r["key"]: r for r in rows}
    closing = by_key["closing"]["effective"]
    if closing:
        for r in rows:
            if r["key"] in co_audit.NOT_BEFORE_CLOSING or not r["effective"]:
                continue
            if r["effective"] > closing:
                r["flags"].append(_flag("bad", f"After the Closing Date ({fmt(closing)})."))
    poss = by_key["possession"]["effective"]
    if closing and poss and poss < closing:
        by_key["possession"]["flags"].append(_flag("warn", f"Before the Closing Date ({fmt(closing)}). Early possession usually needs a separate agreement."))

    # Out of order: within each delivery -> objection -> resolution chain,
    # a later step dated before an earlier one (co_audit.SEQUENCES). Compared
    # as written, like TC Check: a §3.3.3 extension can land both on the same
    # Monday and hide that the contract itself has them backwards.
    for chain in co_audit.SEQUENCES:
        dated = [by_key[k] for k in chain if k in by_key and by_key[k]["date"]]
        for a, b in zip(dated, dated[1:]):
            if b["date"] < a["date"]:
                b["flags"].append(_flag("bad", f"Out of order: before {a['label']} (item {a['item']}, {fmt(a['date'])})."))

    deleted = [r["item"] for r in rows if r["kind"] == "deleted"]
    notes = []
    if not closing:
        notes.append("No Closing Date entered, so the \u201cafter closing\u201d check was skipped.")
    for r in rows:
        r["date_display"] = fmt(r["date"]) if r["date"] else ""
        r["effective_display"] = fmt(r["effective"]) if r["effective"] else ""
        r["date"] = r["date"].isoformat() if r["date"] else None
        r["effective"] = r["effective"].isoformat() if r["effective"] else None
    result = {"mec": mec.isoformat(), "mec_display": fmt(mec), "extend": extend, "rows": rows,
              "deleted_items": deleted, "flagged": sum(1 for r in rows if r["kind"] != "deleted" and r["flags"]),
              "dated": sum(1 for r in rows if r["effective"]), "notes": notes}
    result["text"] = to_text(result)
    return result


_EXTEND_TEXT = {"will": "\u201cWill\u201d be extended (§3.3.3)", "will_not": "\u201cWill Not\u201d be extended (§3.3.3)",
                "": "neither box checked, so not extended (§3.3.3)"}


def to_text(result: dict) -> str:
    """Plain text for pasting into an email."""
    lines = ["Colorado CBS1 \u00a73.1 deadlines", f"MEC: {result['mec_display']}",
             f"Weekends and holidays: {_EXTEND_TEXT[result['extend']]}", ""]
    for r in result["rows"]:
        if r["kind"] == "deleted":
            continue
        if r["kind"] == "invalid":
            when = f"couldn't read \"{r['input']}\""
        else:
            when = r["effective_display"] + (f" (MEC + {r['days']})" if r["kind"] == "days" else "")
            if r["effective"] != r["date"]:
                when += f", extended from {r['date_display']}"
        lines.append(f"{r['item']}. {r['label']}: {when}")
        lines += [f"   ! {f['text']}" for f in r["flags"]]
    if result["deleted_items"]:
        lines += ["", "Blank, N/A or Deleted (\u00a73.2: the provision containing the deadline is deleted): items "
                  + ", ".join(str(i) for i in result["deleted_items"])]
    lines += ["", SOURCE_NOTE, "Free CBS1 deadline calculator: https://txtanoffer.com/tools/colorado-deadlines"]
    return "\n".join(lines)


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> list:
    """RFC 5545 §3.1: lines over 75 octets continue on the next line after
    CRLF + one space. Cut on UTF-8 boundaries so "§" isn't split."""
    out, cur = [], ""
    for ch in line:
        limit = 75 if not out else 74
        if len((cur + ch).encode("utf-8")) > limit:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return [out[0]] + [" " + x for x in out[1:]]


def to_ics(result: dict, now: datetime = None) -> str:
    """All-day events, one per dated row. Deterministic UIDs (MEC + item +
    date), so re-importing an unchanged schedule updates rather than
    duplicates in most calendar apps."""
    stamp = (now or datetime.utcnow()).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TxtAnOffer//CBS1 Deadline Calculator//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:CBS1 deadlines"]
    for r in result["rows"]:
        if not r["effective"]:
            continue
        d = date.fromisoformat(r["effective"])
        uid = hashlib.sha1(f"{result['mec']}|{r['item']}|{r['effective']}".encode()).hexdigest()[:20]
        desc = [f"CBS1 \u00a73.1 item {r['item']} ({r['ref']}). MEC {result['mec_display']}."]
        desc += [f["text"] for f in r["flags"]]
        desc += [TIME_NOTE, SOURCE_NOTE]
        lines += ["BEGIN:VEVENT", f"UID:{uid}@txtanoffer.com", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  "SUMMARY:" + _ics_escape(f"{r['label']} (CBS1 item {r['item']})"),
                  "DESCRIPTION:" + _ics_escape(" ".join(desc)), "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(x for line in lines for x in _fold(line)) + "\r\n"


def from_args(args) -> tuple:
    """(result, error) from request args: mec=YYYY-MM-DD, ext=will|will_not,
    and one d_<row key> per row."""
    mec = co_audit._parse_date(args.get("mec", ""))
    if not mec or not (2000 <= mec.year <= 2100):
        return None, "Enter the MEC date (the date both parties signed)."
    entries = {k: args.get("d_" + k, "") for k in ROW_KEYS}
    return compute(mec, entries, args.get("ext", "")), None


# --- Markup shared by /tools/colorado-deadlines and the embed -------------
# One calculator, two shells. The table is the input form and the output:
# each §3.1 row gets its computed date and flags under the entry, so a TC
# reads it in the contract's own order.

CALC_CSS = """
.cdc{--a:#0b5d52;--ad:#0a3a33;--t:#0f1f2f;--m:#5a6b7a;--b:rgba(15,31,47,.12);--warn:#8a5a00;--warnbg:#FFF6E5;--bad:#9b1c1c;--badbg:#FDECEC;color:var(--t);font-size:15px;line-height:1.45}
.cdc *{box-sizing:border-box}
.cdc-top{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr);gap:.9rem 1.2rem;align-items:start;margin-bottom:.9rem}
.cdc label.cdc-l,.cdc legend{display:block;font-size:.8rem;font-weight:700;margin-bottom:.3rem;padding:0}
.cdc input[type=date],.cdc input[type=text]{width:100%;padding:.55rem .65rem;border:1px solid #cfd6dc;border-radius:.5rem;font:inherit;font-size:16px;background:#fff;color:var(--t)}
.cdc input:focus{outline:2px solid var(--a);outline-offset:1px;border-color:var(--a)}
.cdc fieldset{border:0;margin:0;padding:0;min-width:0}
.cdc-radios{display:flex;flex-wrap:wrap;gap:.35rem .9rem;font-size:.85rem}
.cdc-radios label{display:inline-flex;align-items:center;gap:.3rem;cursor:pointer}
.cdc-hint{font-size:.8rem;color:var(--m);margin:.15rem 0 .8rem}
.cdc-g{font-size:.72rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--ad);background:#E7F3F1;padding:.35rem .6rem;border-radius:.4rem;margin:.8rem 0 .2rem}
.cdc-r{display:grid;grid-template-columns:2.1rem minmax(0,1fr) 8.5rem minmax(0,11rem);gap:.25rem .6rem;align-items:center;padding:.35rem .2rem;border-bottom:1px solid var(--b)}
.cdc-n{font-size:.78rem;font-weight:700;color:var(--m)}
.cdc-lb{font-size:.86rem}
.cdc-lb small{color:var(--m);font-size:.72rem;margin-left:.25rem}
.cdc-o{font-size:.86rem;font-weight:700;white-space:nowrap}
.cdc-o.del{font-weight:500;color:var(--m)}
.cdc-f{grid-column:2/-1;display:flex;flex-direction:column;gap:.2rem}
.cdc-f:empty{display:none}
.cdc-f span{font-size:.78rem;padding:.25rem .5rem;border-radius:.35rem;background:var(--warnbg);color:var(--warn)}
.cdc-f span.bad{background:var(--badbg);color:var(--bad)}
.cdc-f span.info{background:#f1f3f5;color:var(--m)}
.cdc-run{display:block;width:100%;margin:1rem 0 .4rem;padding:.8rem 1rem;border:0;border-radius:999px;background:var(--a);color:#fff;font:inherit;font-weight:700;font-size:1rem;cursor:pointer}
.cdc-run:hover{background:#16806e}
.cdc-err{color:var(--bad);font-size:.85rem;font-weight:600;min-height:1.2em}
.cdc-sum{display:none;background:#E7F3F1;border-radius:.6rem;padding:.75rem .9rem;margin:.5rem 0 .2rem;font-size:.86rem}
.cdc-sum b{color:var(--ad)}
.cdc-act{display:none;flex-wrap:wrap;gap:.5rem;margin-top:.6rem}
.cdc-act a,.cdc-act button{flex:1 1 10rem;text-align:center;padding:.6rem .9rem;border:1px solid var(--a);border-radius:999px;background:#fff;color:var(--a);font:inherit;font-size:.88rem;font-weight:700;text-decoration:none;cursor:pointer}
.cdc-notes{font-size:.75rem;color:var(--m);margin-top:.8rem}
.cdc-notes p{margin:.25rem 0}
@media(max-width:560px){
 .cdc-top{grid-template-columns:1fr}
 .cdc-r{grid-template-columns:1.9rem minmax(0,1fr) minmax(0,8rem)}
 .cdc-o{grid-column:2/-1;white-space:normal}
 .cdc-o:empty{display:none}
}
"""


def calc_markup(surface: str = "page") -> str:
    from html import escape
    parts, group = [], None
    for key, item, ref, grp, label in ROWS:
        if grp != group:
            group = grp
            if grp:
                parts.append(f'<div class="cdc-g">{escape(grp)}</div>')
        parts.append(
            f'<div class="cdc-r" data-key="{key}"><span class="cdc-n">{item}</span>'
            f'<label class="cdc-lb" for="cdc_{key}">{escape(label)}<small>{escape(ref)}</small></label>'
            f'<input type="text" id="cdc_{key}" name="d_{key}" inputmode="text" autocomplete="off" placeholder="days or date">'
            f'<span class="cdc-o" id="cdco_{key}"></span><div class="cdc-f" id="cdcf_{key}"></div></div>')
    return f"""<div class="cdc" id="cdc" data-surface="{surface}">
  <div class="cdc-top">
    <div><label class="cdc-l" for="cdcMec">MEC: the date both parties signed</label><input type="date" id="cdcMec" name="mec"></div>
    <fieldset><legend>&sect;3.3.3: if a deadline falls on a weekend or holiday, it&hellip;</legend>
      <div class="cdc-radios">
        <label><input type="radio" name="ext" value="" checked> Neither box checked</label>
        <label><input type="radio" name="ext" value="will"> Will be extended</label>
        <label><input type="radio" name="ext" value="will_not"> Will Not be extended</label>
      </div></fieldset>
  </div>
  <p class="cdc-hint">For each &sect;3.1 row, type what the contract says: days after MEC (e.g. <b>10</b>), a date (<b>11/20/2026</b>), or leave it blank / N/A.</p>
  {''.join(parts)}
  <button type="button" class="cdc-run" id="cdcRun" data-evt="tool_calc_run">Calculate deadlines</button>
  <div class="cdc-err" id="cdcErr" role="alert"></div>
  <div class="cdc-sum" id="cdcSum" aria-live="polite"></div>
  <div class="cdc-act" id="cdcAct">
    <a id="cdcIcs" href="#" download="cbs1-deadlines.ics" data-evt="tool_ics">Add to calendar (.ics)</a>
    <button type="button" id="cdcCopy" data-evt="tool_copy_text">Copy as text</button>
  </div>
  <div class="cdc-notes">
    <p>{escape(SEC_3_2)}</p>
    <p>{escape(TIME_NOTE)}</p>
    <p>{escape(FHA_VA_NOTE)}</p>
    <p><b>{escape(SOURCE_NOTE)}</b></p>
  </div>
</div>"""


# Plain script, no template braces. `surface` and the embedding host go to
# the server only so /analytics can count runs per site; no entries are
# kept.
CALC_JS = r"""<script>
(function(){
  var root = document.getElementById('cdc'); if(!root) return;
  var surface = root.getAttribute('data-surface') || 'page';
  var host = '';
  try{ if(surface === 'embed' && document.referrer) host = new URL(document.referrer).hostname.replace(/^www\./, ''); }catch(e){}
  var mec = document.getElementById('cdcMec'), err = document.getElementById('cdcErr'), sum = document.getElementById('cdcSum');
  var act = document.getElementById('cdcAct'), ics = document.getElementById('cdcIcs'), copyBtn = document.getElementById('cdcCopy');
  var last = null, ran = false, logged = false, timer = null;
  function esc(t){ return String(t).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function params(){
    var p = new URLSearchParams();
    p.set('mec', mec.value);
    var ext = root.querySelector('input[name=ext]:checked'); p.set('ext', ext ? ext.value : '');
    root.querySelectorAll('.cdc-r input').forEach(function(i){ if(i.value.trim()) p.set(i.name, i.value.trim()); });
    return p;
  }
  function render(r){
    r.rows.forEach(function(row){
      var o = document.getElementById('cdco_' + row.key), f = document.getElementById('cdcf_' + row.key);
      o.className = 'cdc-o' + (row.kind === 'deleted' ? ' del' : '');
      o.textContent = row.kind === 'deleted' ? 'Deleted (§3.2)' : row.kind === 'invalid' ? '?' : row.effective_display;
      f.innerHTML = row.kind === 'deleted' ? '' : row.flags.map(function(x){ return '<span class="' + x.level + '">' + esc(x.text) + '</span>'; }).join('');
    });
    var d = r.deleted_items.length;
    sum.innerHTML = '<b>' + r.dated + '</b> date' + (r.dated === 1 ? '' : 's') + ' calculated from MEC ' + esc(r.mec_display) +
      ' &middot; <b>' + r.flagged + '</b> need' + (r.flagged === 1 ? 's' : '') + ' a look' +
      (d ? ' &middot; <b>' + d + '</b> row' + (d === 1 ? '' : 's') + ' left blank: if the contract also has them blank, N/A or Deleted, §3.2 deletes those provisions.' : '') +
      r.notes.map(function(n){ return '<br>' + esc(n); }).join('');
    sum.style.display = 'block'; act.style.display = r.dated ? 'flex' : 'none';
  }
  function run(log){
    err.textContent = '';
    if(!mec.value){ err.textContent = 'Enter the MEC date (the date both parties signed).'; mec.focus(); return; }
    var p = params(); p.set('surface', surface); if(host) p.set('host', host);
    ics.href = '/tools/colorado-deadlines.ics?' + p.toString();
    if(log && !logged){ p.set('log', '1'); logged = true; }
    fetch('/v1/tools/co-deadlines?' + p.toString(), {credentials: surface === 'embed' ? 'omit' : 'same-origin'})
      .then(function(res){ return res.json(); })
      .then(function(r){ if(r.error){ err.textContent = r.error; return; } last = r; ran = true; render(r); })
      .catch(function(){ err.textContent = 'Couldn’t reach the calculator. Check your connection and try again.'; });
  }
  document.getElementById('cdcRun').addEventListener('click', function(){ run(true); });
  root.addEventListener('input', function(){ if(!ran) return; clearTimeout(timer); timer = setTimeout(function(){ run(false); }, 350); });
  root.addEventListener('change', function(e){ if(ran && e.target.type !== 'text') run(false); });
  copyBtn.addEventListener('click', function(){
    if(!last) return;
    var done = function(){ copyBtn.textContent = 'Copied'; setTimeout(function(){ copyBtn.textContent = 'Copy as text'; }, 1800); };
    var fallback = function(){ var t = document.createElement('textarea'); t.value = last.text; t.setAttribute('readonly', ''); t.style.position = 'fixed'; t.style.opacity = '0';
      document.body.appendChild(t); t.select(); try{ document.execCommand('copy'); done(); }catch(e){} t.remove(); };
    if(navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(last.text).then(done, fallback); else fallback();
  });
  var foot = document.getElementById('cdcPowered');
  if(foot) foot.href = 'https://txtanoffer.com/tools/colorado-deadlines?src=embed' + (host ? '_' + host.replace(/[^a-z0-9-]+/gi, '-').toLowerCase().slice(0, 50) : '');
})();
</script>"""


EMBED_HEIGHT = 1100   # measured at 360-640px wide; the iframe scrolls past that
EMBED_SNIPPET = ('<iframe src="https://txtanoffer.com/embed/colorado-deadlines" width="100%" '
                 f'height="{EMBED_HEIGHT}" style="border:0" title="Colorado CBS1 deadline calculator"></iframe>')

PAGE_TITLE = "Colorado CBS1 Deadline Calculator (§3.1): Free, No Upload"
PAGE_DESCRIPTION = ("Free Colorado CBS1 deadline calculator. Enter MEC and the §3.1 entries and get every deadline as a "
                    "date, with weekends, Colorado holidays (incl. Cabrini Day), deadlines after closing and out-of-order "
                    "dates flagged. No upload, nothing saved.")

PAGE_CSS = """<style>
.tool-card{background:#fff;border:1px solid var(--border);border-radius:var(--radius);padding:1.4rem;box-shadow:0 1px 3px rgba(15,31,47,.05);margin:1.25rem 0 1.5rem}
.tool-cta{background:var(--accent-tint, #E7F3F1);border-radius:var(--radius-sm);padding:1.2rem 1.3rem;margin:0 0 2.5rem}
.tool-cta strong{display:block;font-size:1.02rem;margin-bottom:.6rem}
.tool-cta .btn{display:inline-block;background:var(--accent);color:#fff;padding:.7rem 1.4rem;border-radius:9999px;font-weight:600;text-decoration:none}
.tool-rules li{margin:0 0 .6rem 1.1rem;font-size:.9rem;color:var(--text-muted)}
.tool-rules q{color:var(--text)}
.embed-box{background:#fff;border:1px solid var(--border);border-radius:var(--radius-sm);padding:1rem}
.embed-box pre{white-space:pre-wrap;word-break:break-all;font-size:.78rem;background:#f5f5f7;padding:.75rem;border-radius:.5rem;margin:.5rem 0 .75rem}
.embed-box button{padding:.55rem 1.1rem;border-radius:9999px;border:1px solid var(--accent);background:#fff;color:var(--accent);font:inherit;font-weight:700;cursor:pointer}
@media(max-width:600px){.tool-card{padding:1rem .8rem}.tool-cta .btn{display:block;text-align:center}}
</style>"""


def page_body() -> str:
    from html import escape
    return f"""
<div class="container">
  <div class="page-header">
    <h1>Colorado CBS1 deadline calculator</h1>
    <p>Enter MEC and what the contract says in each &sect;3.1 row. You get every deadline as a date, with weekends, federal and Colorado holidays, deadlines after closing and out-of-order dates flagged. No upload, nothing saved.</p>
  </div>

  <div class="tool-card">{calc_markup("page")}</div>

  <div class="tool-cta">
    <strong>Check the whole contract: forward the CBS1 to tc@check.txtanoffer.com</strong>
    <a class="btn" href="/tc-check?state=co&amp;src=tool_co_deadlines" data-evt="tool_check_cta">Check a file free &rarr;</a>
  </div>

  <div class="section">
    <h2>How the dates are counted</h2>
    <ul class="tool-rules">
      <li><b>Days after MEC.</b> MEC is &ldquo;the date upon which both parties have signed this Contract&rdquo; (&sect;3.2). For a number of days, <q>the first day is excluded and the last day is included</q> (&sect;3.3.2), so MEC + 10 is the tenth calendar day after MEC. The form counts days, not business days.</li>
      <li><b>Weekends and holidays.</b> &sect;3.3.3 has two boxes: a deadline that falls on a Saturday, Sunday or federal or Colorado state holiday <q>Will</q> or <q>Will Not</q> be extended to the next day that isn&rsquo;t one. <q>Should neither box be checked, the deadline will not be extended.</q> The calculator moves a date only when you pick &ldquo;Will&rdquo;. Either way it flags the date so you can confirm it.</li>
      <li><b>Holidays used:</b> the federal holidays (with the federal observed day when one falls on a weekend) plus Cabrini Day, Colorado&rsquo;s first Monday in October.</li>
      <li><b>Closing Date and Possession Date</b> aren&rsquo;t moved. A weekend or holiday closing is flagged so you can confirm the closing company and lender.</li>
      <li><b>Blank, N/A or Deleted.</b> {escape(SEC_3_2)}</li>
    </ul>
  </div>

  <div class="section">
    <h2>Add this to your website</h2>
    <p class="section-sub">Free for brokerage and TC websites. Paste this where you want the calculator to appear. It collects no names or contract details and sets no cookies.</p>
    <div class="embed-box">
      <pre id="embedCode">{escape(EMBED_SNIPPET)}</pre>
      <button type="button" id="embedCopy" data-evt="tool_embed_copy">Copy embed code</button>
    </div>
  </div>

  <div class="disclaimer-box"><strong>{escape(SOURCE_NOTE)}</strong> TxtAnOffer is an independent tool, not affiliated with or endorsed by the Colorado Real Estate Commission or the Colorado Division of Real Estate. Item numbers and labels follow the CBS1 &ldquo;for use on or after January 1, 2026&rdquo;.</div>

  <p class="foot"><a href="/tc-hub/colorado">&larr; Colorado TC Hub</a> &middot; <a href="/?state=co">Home</a> &middot; <a href="/terms">Terms</a></p>
</div>
{CALC_JS}
<script>
document.getElementById('embedCopy').addEventListener('click', function(){{
  var b = this, code = document.getElementById('embedCode').textContent;
  var done = function(){{ b.textContent = 'Copied'; setTimeout(function(){{ b.textContent = 'Copy embed code'; }}, 1800); }};
  if(navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(code).then(done);
  else {{ var r = document.createRange(); r.selectNodeContents(document.getElementById('embedCode')); var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); try{{ document.execCommand('copy'); done(); }}catch(e){{}} }}
}});
</script>
</body>
</html>"""


def embed_page() -> str:
    """Self-contained iframe widget: no site nav, no web fonts, no cookies,
    no page-view beacon (that one sets ta_vid)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Colorado CBS1 deadline calculator</title>
<meta name="robots" content="noindex">
<link rel="canonical" href="https://txtanoffer.com/tools/colorado-deadlines">
<style>
html,body{{margin:0;padding:0;background:#fff}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased;padding:14px 12px 10px}}
h1{{font-size:1.05rem;margin:0 0 .7rem;color:#0f1f2f;letter-spacing:-.01em}}
.pw{{margin-top:1rem;padding-top:.7rem;border-top:1px solid rgba(15,31,47,.12);font-size:.75rem;text-align:center;color:#5a6b7a}}
.pw a{{color:#0b5d52;font-weight:700;text-decoration:none}}
{CALC_CSS}
</style>
</head>
<body>
<h1>Colorado CBS1 deadline calculator</h1>
{calc_markup("embed")}
<div class="pw"><a id="cdcPowered" href="https://txtanoffer.com/tools/colorado-deadlines?src=embed" target="_blank" rel="noopener">Powered by TxtAnOffer &middot; Free CBS1 deadline calculator</a></div>
{CALC_JS}
</body>
</html>"""
