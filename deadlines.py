"""
deadlines.py -- Computes the dates that matter once a TREC 20-19 offer is
actually accepted: the Effective Date, the Paragraph 5A earnest money /
option fee delivery deadline (3 days after the Effective Date), and the
option period's own termination date (if the agent specified one).

What moves and what doesn't (TREC 20-19, 05-04-2026, read from 20-19_2.pdf):
20-19 extends only 5A(2) deliveries (earnest money, option fee, additional
earnest money) past a Saturday, Sunday or Legal Holiday. 9A and 5B are not
extended. The 9A roll is TxtAnOffer's drafting default so the printed date
is a funding day. So:
  - earnest_money_deadline(): Effective Date + 3, extended to the end of the
    next day that isn't a Saturday, Sunday or Texas Legal Holiday
    (_tx_legal_holidays below, the form's own definition).
  - option_end_date(): Effective Date + option days, 5:00 PM local time,
    never moved. A 5B period that ends on a Saturday ends on that Saturday;
    rolling it (as this module did until 2026-10-24) told a buyer they had
    until Monday when the unrestricted right to terminate was already gone.
  - offer_closing_date() / add_trec_days(): our drafting default for the 9A
    date we print, off weekends and US federal holidays.

Effective Date here is the date the listing agent clicks Accept on the
/thread/<filename> page (offers.thread_responded_at) -- this app has no
separate "date of final acceptance" field, and treating the accept click as
that date is the closest available proxy, not a verified legal reading.

Two holiday sets, both computed by rule (not a maintained date list) so they
keep working in future years without upkeep, on their actual dates with no
"observed" Monday shift:
  - _federal_holidays(): US federal holidays, for the 9A drafting roll (and
    the 39-11 date built on it) -- the days title and lenders don't fund.
  - _tx_legal_holidays(): the 20-19's "Legal Holiday" -- Tex. Gov't Code
    §662.003(a) national holidays plus (b)(4) June 19 and (b)(6) the Friday
    after Thanksgiving. No Columbus Day (it's federal, not in §662.003(a)).
    Used only for 5A(2) deliveries. Same list as static/trec-deadlines.js
    (the public deadline calculator), so the two can't disagree.

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

Offers generated before GENERATOR_VERSION 2026-10-24.1 printed the old date
(naive-UTC created_at date + close_days, never rolled: the filler ran on
Railway's UTC clock), and that PDF is what the parties have. Callers that
read an offer row pass its generator_version so every page, the validator,
the reminders and a 39-11 keep agreeing with the PDF that actually exists.
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


def _tx_legal_holidays(year: int) -> set:
    """The 20-19's "Legal Holiday" for 5A(2): Tex. Gov't Code §662.003(a)
    national holidays plus (b)(4) June 19 and (b)(6) the Friday after
    Thanksgiving. Not Columbus Day, which §662.003(a) doesn't list."""
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        date(year, 1, 1),               # New Year's Day
        _nth_weekday(year, 1, 0, 3),     # Martin Luther King, Jr., Day
        _nth_weekday(year, 2, 0, 3),     # Presidents' Day
        _last_weekday(year, 5, 0),       # Memorial Day
        date(year, 6, 19),               # Emancipation Day in Texas, (b)(4)
        date(year, 7, 4),                # Independence Day
        _nth_weekday(year, 9, 0, 1),     # Labor Day
        date(year, 11, 11),              # Veterans Day
        thanksgiving,                    # Thanksgiving Day
        thanksgiving + timedelta(days=1),  # Friday after Thanksgiving, (b)(6)
        date(year, 12, 25),              # Christmas Day
    }


def is_business_day(d: date) -> bool:
    """Not a Saturday, Sunday or US federal holiday: the 9A drafting roll."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return d not in _federal_holidays(d.year)


def is_5a2_delivery_day(d: date) -> bool:
    """Not a Saturday, Sunday or Texas Legal Holiday: the 5A(2) rule."""
    if d.weekday() >= 5:
        return False
    return d not in _tx_legal_holidays(d.year)


def add_trec_days(start: date, days: int) -> date:
    """start + `days` calendar days, then moved forward to the next day that
    isn't a Saturday, Sunday or US federal holiday. This is TxtAnOffer's 9A
    drafting default (and the 39-11 closing date built on it), not a 20-19
    rule: 20-19 extends only 5A(2) deliveries (earnest money, option fee,
    additional earnest money) past a Saturday, Sunday or Legal Holiday. 9A
    and 5B are not extended. The 9A roll is TxtAnOffer's drafting default so
    the printed date is a funding day. Don't use it for 5A or 5B."""
    d = start + timedelta(days=days)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def add_5a2_days(start: date, days: int) -> date:
    """Paragraph 5A(2): start + `days`, and if that day is a Saturday, Sunday
    or Legal Holiday, the end of the next day that isn't one."""
    d = start + timedelta(days=days)
    while not is_5a2_delivery_day(d):
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


try:
    CO_TZ = ZoneInfo("America/Denver")
except Exception:
    CO_TZ = timezone(timedelta(hours=-7))


def colorado_date(moment=None) -> date:
    """texas_date() for a Colorado CBS1: the calendar date in Denver. A 9 PM
    Mountain text is still today there, though UTC (and Railway's
    datetime.now()) has already moved to tomorrow."""
    if moment is None:
        moment = _utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CO_TZ).date()


# The GENERATOR_VERSION that started printing offer_closing_date() on the
# 20-19. Offers made by an older generator printed _legacy_closing_date().
ROLLED_CLOSING_SINCE = "2026-10-24.1"


def _is_legacy(generator_version) -> bool:
    """True for an offer row made before ROLLED_CLOSING_SINCE. None means the
    caller isn't reading a stored row (drafting right now), so not legacy. An
    empty string is a row from before the generator_version column existed
    (2026-08) -- record_offer has stamped every row since, and versions are
    "YYYY-MM-DD.N", so a plain string compare orders them."""
    if generator_version is None:
        return False
    return str(generator_version) < ROLLED_CLOSING_SINCE


def _legacy_closing_date(close_days, drafted_at) -> date:
    """What the old filler printed: the UTC date of created_at (stored naive
    UTC; Railway's clock is UTC) + close_days, no TZ conversion, no roll."""
    moment = datetime.fromisoformat(drafted_at) if isinstance(drafted_at, str) else drafted_at
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.date() + timedelta(days=int(close_days or 0))


def offer_closing_date(close_days, drafted_at=None, generator_version=None) -> date:
    """Paragraph 9A closing date for an offer drafted at `drafted_at` (the
    offer's created_at; None = being drafted right now) with `close_days`
    to close: the Texas drafting date + close_days, moved forward off a
    Saturday, Sunday or federal holiday. 20-19 extends only 5A(2) deliveries
    (earnest money, option fee, additional earnest money) past a Saturday,
    Sunday or Legal Holiday. 9A and 5B are not extended. The 9A roll is
    TxtAnOffer's drafting default so the printed date is a funding day
    (co_filler.contract_dates does the same for CBS1 closings).

    Pass the row's `generator_version`: an offer generated before
    ROLLED_CLOSING_SINCE gets the date its PDF actually printed
    (_legacy_closing_date). An unreadable created_at falls back to now
    rather than crashing a page."""
    if _is_legacy(generator_version) and drafted_at:
        try:
            return _legacy_closing_date(close_days, drafted_at)
        except (TypeError, ValueError, AttributeError):
            pass
    try:
        start = texas_date(drafted_at)
    except (TypeError, ValueError):
        start = texas_date()
    return add_trec_days(start, int(close_days or 0))


def roll_note(unrolled: date, business_day=None) -> str:
    """Why a date rolled off `unrolled`, for an SMS summary: "" when it isn't
    moved, else "moved off the weekend" / "moved off a holiday" / "moved off
    the weekend and a holiday". `business_day` defaults to is_business_day
    (the 9A drafting roll); Colorado passes co_audit.is_business_day, which
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


def closing_roll_note(close_days, drafted_at=None, generator_version=None) -> str:
    """roll_note() for offer_closing_date(close_days, drafted_at). Without it
    the SMS summary read "Nov 16 (21 days)" for a date 23 days out. Legacy
    offers were never rolled, so they never get a note."""
    if _is_legacy(generator_version) and drafted_at:
        return ""
    try:
        start = texas_date(drafted_at)
    except (TypeError, ValueError):
        start = texas_date()
    return roll_note(start + timedelta(days=int(close_days or 0)))


def fmt_closing_date(d: date) -> str:
    """'November 16, 2026' -- the long form the review/demo pages show
    (no zero-padded day, unlike strftime's %d)."""
    return f"{d:%B} {d.day}, {d.year}"


def earnest_money_deadline(effective_date: date, days: int = 3) -> date:
    """Paragraph 5A: earnest money and option fee due within 3 days after
    the Effective Date (additional earnest money: pass its own `days`),
    extended per 5A(2) past a Saturday, Sunday or Texas Legal Holiday --
    the one 20-19 deadline that is extended."""
    return add_5a2_days(effective_date, days)


def option_end_date(effective_date: date, option_days) -> date:
    """Paragraph 5B: the option period's unrestricted-termination deadline,
    Effective Date + option days, 5:00 PM local time where the property is.
    Never moved for a weekend or holiday -- 5B isn't extended. None if no
    option period was specified on this offer."""
    if not option_days:
        return None
    return effective_date + timedelta(days=int(option_days))


def fmt_option_end(d: date) -> str:
    """How every surface shows the 5B date, so none of them implies it moves:
    '5:00 PM local time, Sat, Oct 31, 2026 (Par. 5B, not extended for
    weekends/holidays)'."""
    return f"5:00 PM local time, {d:%a}, {d:%b} {d.day}, {d.year} (Par. 5B, not extended for weekends/holidays)"


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
        lines.append(f"Option period ends: {fmt_option_end(end)}")
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
