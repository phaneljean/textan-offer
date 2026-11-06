"""
tc_deadlines.py -- Deadline Autopilot (free tier) for TC Check.

Strategy (Phanel, 2026-11-05): the deadline sheet is the FREE acquisition
feature -- forward a signed contract, get every deadline back, plus a
calendar file that puts them in the agent's own calendar. Advanced
monitoring (reminder/escalation sequences) and brokerage-wide deadline
oversight are the paid tiers; they are deliberately NOT built here.

Input is tc_audit.check_tc_file()'s "key_dates" -- the dates and amounts
as written on the TREC 20-19 itself. The math is deadlines.py's, shared
with the text-an-offer drafting side so the two can never disagree:

  - Earnest money / option fee delivery: Par. 5A -- within 3 days after
    the Effective Date, extended past a Saturday, Sunday or Texas Legal
    Holiday per 5A(2) (deadlines.earnest_money_deadline). This is the one
    20-19 deadline the form itself extends.
  - Option period end: Par. 5B -- Effective Date + the days written on
    the form, 5:00 PM local time, NEVER extended (deadlines.option_end_date).
  - Seller's Disclosure Notice: Par. 7B(2) -- within the days written on
    the form after the Effective Date, calendar days as written. Only
    computed when 7B(2) is the box checked on the contract (key_dates
    leaves disclosure_days None otherwise: 7B(1) means already delivered).
  - Closing: Par. 9A -- the date as written on the contract, shown as-is.

Honesty rules, same as the rest of TC Check: a date that can't be read
from the form is never guessed. If the Effective Date is blank nothing is
computed (the findings already flag it). If the option fee is filled in
but the option-period days are blank, the sheet says so instead of
inventing an end date. Every surface that shows these dates carries the
"computed from the contract as written -- verify against the executed
copy" line.

The calendar file is a plain RFC 5545 .ics: one all-day event per date
deadline (alarm the day before) and a timed 5:00 PM event for the option
period end (alarms 1 day and 3 hours before). No server-side state, no
retention -- the file is generated inside the same pass that reads the
contract and is attached to the report email, matching the product's
read-then-deleted promise.
"""
from datetime import date, datetime, timedelta, timezone

import deadlines as _deadlines

# Option period ends at 5:00 PM where the property is -- Texas contracts,
# Texas time (deadlines.TX_TZ is America/Chicago with a safe fallback).
_OPTION_TZ_NAME = "America/Chicago"


def _as_date(value):
    """ISO 'YYYY-MM-DD' string (or a date) -> date, else None."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _money(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_money(x) -> str:
    return f"${x:,.0f}" if float(x) == int(x) else f"${x:,.2f}"


def compute_deadlines(key_dates: dict) -> list:
    """The deadline sheet for one checked 20-19, from its key_dates.

    Returns a list of dicts, dated entries first (earliest first), then
    entries that could not be computed:
      {key, label, date (date|None), time_label (str|None),
       basis, detail, pending_reason (str|None)}
    Empty list when there is no Effective Date to count from."""
    kd = key_dates or {}
    eff = _as_date(kd.get("effective_date"))
    if eff is None:
        return []

    earnest = _money(kd.get("earnest_money"))
    option_fee = _money(kd.get("option_fee"))
    items = []

    # Par. 5A -- earnest money + option fee delivery.
    money_bits = []
    if earnest is not None:
        money_bits.append(f"{_fmt_money(earnest)} earnest money")
    if option_fee is not None:
        money_bits.append(f"{_fmt_money(option_fee)} option fee")
    items.append({
        "key": "earnest_money",
        "label": "Earnest money + option fee due to the escrow agent",
        "date": _deadlines.earnest_money_deadline(eff),
        "time_label": None,
        "basis": "Par. 5A: within 3 days after the Effective Date, extended past a "
                 "Saturday, Sunday or Texas Legal Holiday per Par. 5A(2)",
        "detail": "Deliver: " + " + ".join(money_bits) if money_bits else "",
        "pending_reason": None,
    })

    # Par. 5B -- option period end, 5:00 PM, never extended.
    option_days = kd.get("option_days")
    if option_days:
        detail = ("At 5:00 PM the buyer's unrestricted right to terminate expires."
                  if option_fee is None else
                  f"The {_fmt_money(option_fee)} option fee buys the unrestricted right to "
                  "terminate until this moment; after it, that right is gone.")
        items.append({
            "key": "option_end",
            "label": "Option period ends",
            "date": _deadlines.option_end_date(eff, option_days),
            "time_label": "5:00 PM",
            "basis": f"Par. 5B: Effective Date + {int(option_days)} days, 5:00 PM local "
                     "time, not extended for weekends or holidays",
            "detail": detail,
            "pending_reason": None,
        })
    elif option_fee is not None:
        items.append({
            "key": "option_end",
            "label": "Option period ends",
            "date": None,
            "time_label": None,
            "basis": "Par. 5B",
            "detail": "",
            "pending_reason": "not computed: the option-period days are blank in Par. 5B "
                              "(flagged in the findings above)",
        })

    # Par. 7B(2) -- Seller's Disclosure Notice delivery.
    disclosure_days = kd.get("disclosure_days")
    if disclosure_days:
        items.append({
            "key": "disclosure",
            "label": "Seller's Disclosure Notice due from Seller",
            "date": eff + timedelta(days=int(disclosure_days)),
            "time_label": None,
            "basis": f"Par. 7B(2): within {int(disclosure_days)} days after the Effective "
                     "Date, as written",
            "detail": "",
            "pending_reason": None,
        })

    # Par. 9A -- closing, the date as written.
    closing = _as_date(kd.get("closing_date"))
    if closing is not None:
        items.append({
            "key": "closing",
            "label": "Closing (on or before)",
            "date": closing,
            "time_label": None,
            "basis": "Par. 9A: the date written on the contract",
            "detail": (f"{_fmt_money(earnest)} earnest money is on the line at closing."
                       if earnest is not None else ""),
            "pending_reason": None,
        })

    dated = sorted((i for i in items if i["date"] is not None), key=lambda i: i["date"])
    pending = [i for i in items if i["date"] is None]
    return dated + pending


def fmt_day(d: date) -> str:
    """'Fri, Nov 13, 2026' -- the sheet's one date format."""
    return f"{d:%a}, {d:%b} {d.day}, {d.year}"


# --- Calendar file -----------------------------------------------------------

def _ics_escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> list:
    """RFC 5545 folding: no content line over 75 octets (continuation
    lines start with a space). Our content is ASCII by construction
    except addresses, so fold on characters with a small safety margin."""
    if len(line) <= 73:
        return [line]
    out, rest = [line[:73]], line[73:]
    while rest:
        out.append(" " + rest[:72])
        rest = rest[72:]
    return out


def build_ics(items: list, property_label: str = "") -> str:
    """An .ics calendar with one event per dated deadline. Pending
    (uncomputed) entries are skipped -- a calendar must never carry a
    guessed date."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    where = f" -- {property_label}" if property_label else ""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TxtAnOffer//Deadline Autopilot//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:TxtAnOffer deadlines",
    ]
    for item in items:
        d = item["date"]
        if d is None:
            continue
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{item['key']}-{d:%Y%m%d}@txtanoffer.com")
        lines.append(f"DTSTAMP:{now}")
        if item["key"] == "option_end":
            lines.append(f"DTSTART;TZID={_OPTION_TZ_NAME}:{d:%Y%m%d}T170000")
            lines.append(f"DTEND;TZID={_OPTION_TZ_NAME}:{d:%Y%m%d}T173000")
            alarms = ["-P1D", "-PT3H"]
        else:
            lines.append(f"DTSTART;VALUE=DATE:{d:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}")
            alarms = ["-P1D"]
        lines.append("SUMMARY:" + _ics_escape(f"{item['label']}{where}"))
        desc = item["basis"].rstrip(".") + "."
        if item.get("detail"):
            desc += " " + item["detail"].rstrip(".") + "."
        desc += (" Computed by TxtAnOffer TC Check from the contract as written. "
                 "Verify against the executed copy. General information, not legal advice.")
        lines.append("DESCRIPTION:" + _ics_escape(desc))
        for trigger in alarms:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"DESCRIPTION:{_ics_escape(item['label'])}",
                      f"TRIGGER:{trigger}", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    folded = []
    for line in lines:
        folded.extend(_fold(line))
    return "\r\n".join(folded) + "\r\n"
