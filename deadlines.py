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

Closing date is NOT computed here: it's a hard calendar date fixed at
drafting time (created_at + close_days, see pdf_filler.py) and printed as
such on the contract itself, so it never depends on the Effective Date the
way the earnest money and option deadlines do.
"""
from datetime import date, timedelta


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
