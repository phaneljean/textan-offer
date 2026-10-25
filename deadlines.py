"""
deadlines.py -- Computes the dates that matter once a TREC 20-19 offer is
actually accepted: the Effective Date, the Paragraph 5 earnest money /
option fee delivery deadline (3 days after the Effective Date), and the
option period's own termination date (if the agent specified one) -- all
with TREC's standard day-counting rule applied: a period that lands on a
Saturday, Sunday, or legal holiday extends to the end of the next day that
isn't one.

Effective Date here is the date the listing agent clicks Accept on the
/thread/<filename> page (offers.thread_responded_at) -- this app has no
separate "date of final acceptance" field, and treating the accept click as
that date is the closest available proxy, not a verified legal reading.

Holiday set is US federal holidays only, computed by rule (not a maintained
date list) so it keeps working in future years without upkeep -- this is a
reasonable proxy for "legal holiday," not a verified list of the specific
holidays Texas recognizes for contract day-counting purposes, and doesn't
shift a holiday that falls on a weekend to the nearest weekday the way
"observed" federal holidays do. Good enough to avoid landing a deadline on
Thanksgiving; not a substitute for checking an actual title company's
calendar on a deal that matters.

Closing date doesn't depend on the Effective Date: it's a hard calendar
date fixed at drafting time and printed as such on the contract itself.
offer_closing_date() below is the ONE place it's computed -- the PDF overlay
and cover page (pdf_filler.py, cover_page.py), the validator that gates the
Email button (pdf_validator.py), the review/thread/transaction pages and the
SMS confirmation (app.py), the 39-11 (amendment.py) and the closing reminder
(reminders.py) all call it, so they can't disagree. Found 2026-10-24: each
of those did its own `now/created_at + timedelta(close_days)`, with
pdf_filler on the server's local clock and created_at stored in UTC, so any
offer drafted after ~5 PM Pacific / 7 PM Central on a non-UTC host got one
date on the PDF and the next day on the review page, and the validator
blocked sending.
"""
from datetime import date, datetime, timedelta, timezone


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The nth occurrence (1-indexed) of `weekday` (Mon=0..Sun=6) in a given month."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _federal_holidays(year: int) -> set:
    return {
        date(year, 1, 1),               # New Year's Day
        _nth_weekday(year, 1, 0, 3),     # MLK Day
        _nth_weekday(year, 2, 0, 3),     # Presidents Day
        _last_weekday(year, 5, 0),       # Memorial Day
        date(year, 6, 19),               # Juneteenth
        date(year, 7, 4),                # Independence Day
        _nth_weekday(year, 9, 0, 1),     # Labor Day
        _nth_weekday(year, 10, 0, 2),    # Columbus Day
        date(year, 11, 11),              # Veterans Day
        _nth_weekday(year, 11, 3, 4),    # Thanksgiving
        date(year, 12, 25),              # Christmas
    }


def is_business_day(d: date) -> bool:
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _federal_holidays(d.year)


def add_trec_days(start: date, days: int) -> date:
    """start + `days` calendar days, then rolled forward to the next day
    that isn't a Saturday, Sunday, or (federal) legal holiday -- TREC's
    standard day-counting rule for contract deadlines."""
    d = start + timedelta(days=days)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


# Texas offers are drafted on Texas dates. offers.created_at is naive UTC
# (offers_db.record_offer uses utcnow), so "today" has to be converted, or an
# 8 PM Central text drafts as tomorrow. tzdata missing on the host would make
# ZoneInfo raise; fall back to CST (UTC-6) rather than break offer drafting --
# off by one hour for the DST half of the year, never by a day before 6 PM.
try:
    from zoneinfo import ZoneInfo
    TX_TZ = ZoneInfo("America/Chicago")
except Exception:
    TX_TZ = timezone(timedelta(hours=-6))


def _utcnow() -> datetime:
    """Aware UTC now. Its own function so tests can freeze the clock."""
    return datetime.now(timezone.utc)


def texas_date(moment=None) -> date:
    """The calendar date in Texas at `moment`: a naive datetime (or ISO
    string) is read as UTC, the way created_at is stored; None means now."""
    if moment is None:
        moment = _utcnow()
    elif isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    elif isinstance(moment, date) and not isinstance(moment, datetime):
        return moment  # already a calendar date
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TX_TZ).date()


def offer_closing_date(close_days, drafted_at=None) -> date:
    """Paragraph 9A closing date for an offer drafted at `drafted_at` (the
    offer's created_at; None = being drafted right now) with `close_days`
    to close: the Texas drafting date + close_days, moved forward off a
    Saturday, Sunday or holiday -- a closing can't fund on a day title and
    the lender are shut, and it's the same rule add_trec_days applies to
    every other deadline (and co_filler.contract_dates to CBS1 closings).
    An unreadable created_at falls back to now rather than crashing a page."""
    try:
        start = texas_date(drafted_at)
    except (TypeError, ValueError):
        start = texas_date()
    return add_trec_days(start, int(close_days or 0))


def roll_note(unrolled: date, business_day=None) -> str:
    """Why a date rolled off `unrolled`, for an SMS summary: "" when it isn't
    moved, else "moved off the weekend" / "moved off a holiday" / "moved off
    the weekend and a holiday". `business_day` defaults to is_business_day
    (add_trec_days' rule); Colorado passes co_audit.is_business_day, which
    also knows Colorado holidays."""
    business_day = business_day or is_business_day
    d, weekend, holiday = unrolled, False, False
    while not business_day(d):
        if d.weekday() >= 5:
            weekend = True
        else:
            holiday = True
        d += timedelta(days=1)
    if weekend and holiday:
        return "moved off the weekend and a holiday"
    if weekend:
        return "moved off the weekend"
    if holiday:
        return "moved off a holiday"
    return ""


def closing_roll_note(close_days, drafted_at=None) -> str:
    """roll_note() for offer_closing_date(close_days, drafted_at). Without it
    the SMS summary read "Nov 16 (21 days)" for a date 23 days out."""
    try:
        start = texas_date(drafted_at)
    except (TypeError, ValueError):
        start = texas_date()
    return roll_note(start + timedelta(days=int(close_days or 0)))


def fmt_closing_date(d: date) -> str:
    """'November 16, 2026' -- the long form the review/demo pages show
    (no zero-padded day, unlike strftime's %d)."""
    return f"{d:%B} {d.day}, {d.year}"


def earnest_money_deadline(effective_date: date) -> date:
    """Paragraph 5: earnest money and option fee due within 3 days of the
    Effective Date."""
    return add_trec_days(effective_date, 3)


def option_end_date(effective_date: date, option_days) -> date:
    """The option period's unrestricted-termination deadline, or None if no
    option period was specified on this offer."""
    if not option_days:
        return None
    return add_trec_days(effective_date, option_days)


def build_day_one_summary(address: str, effective_date: date, option_days, close_date) -> list:
    """The plain-text lines of the 'Day One' deadline summary for one
    accepted offer -- shared by the acceptance SMS and the /thread page's
    on-screen summary (app.py) so the two can never drift apart."""
    lines = [
        f"Day One summary — {address}",
        f"Effective Date: {effective_date.strftime('%b %d, %Y')}",
        f"Earnest money + option fee due: {earnest_money_deadline(effective_date).strftime('%b %d, %Y')} (Par. 5)",
    ]
    end = option_end_date(effective_date, option_days)
    if end:
        lines.append(f"Option period ends: {end.strftime('%b %d, %Y')}")
    if close_date:
        lines.append(f"Closing on or before: {close_date.strftime('%b %d, %Y')}")
    lines.append("Share these dates with the buyer, lender, and title company.")
    return lines


def build_co_day_one_summary(address: str, effective_date: date, values: dict) -> list:
    """Colorado version of build_day_one_summary: the key §3.1 dates as
    written on that CBS1 (co_audit.contract_deadlines), not Texas Paragraph
    5/option-period math -- Colorado has no option period. Same shape (first
    line title, last line the share prompt) so callers render it the same."""
    import co_audit
    lines = [f"Day One summary — {address}", f"MEC (both signed): {effective_date.strftime('%b %d, %Y')}"]
    wanted = ("insp_obj", "record_title_obj", "loan_avail", "appraisal_obj")
    picked = [d for d in co_audit.contract_deadlines(values or {}) if d["key"] in wanted]
    for d in sorted(picked, key=lambda d: d["date"] or date.max):
        when = d["date"].strftime('%b %d, %Y') if d["date"] else d["raw"]
        lines.append(f"{d['label']}: {when}")
    closing = co_audit.contract_key_dates(values or {})["closing"]
    if closing:
        lines.append(f"Closing Date: {closing.strftime('%b %d, %Y')}")
    lines.append("Share these dates with the buyer, lender, and title company.")
    return lines
