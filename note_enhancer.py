"""
note_enhancer.py — turn a raw deal note into Deal Sheet proposals.

This module is the ONLY place note text is interpreted into structured
terms. Its public surface is deliberately shaped so a future LLM-backed
implementation can slot in behind the same two functions without any
route or storage code changing:

    enhance_note(raw_text, state, profile) -> proposal dict
    validate_proposal(raw_text, fields, state) -> (fields, warnings)

Phase 1 ships the DETERMINISTIC producer only -- the spec's regex
fallback (section 4.5) standing in as the enhancer until an LLM provider
is chosen. It reuses parser.py's regexes and helpers, makes no network
call, needs no API key, and marks EVERY field it proposes "confirm":
regex extraction from prose is a proposal, never a fact.

`validate_proposal` is where the no-invention rule is enforced for ANY
producer (this one today, an LLM tomorrow):

* Span gate: a field's `span` must be an exact substring of the raw note
  (whitespace/case-normalized). Money/date/party/id fields with an
  unverifiable span are dropped; free-text fields are demoted to
  "confirm" and kept only if the value itself appears in the note.
* Type/range checks demote to "confirm" -- values are never silently
  "fixed".
* Seller concessions whose own words say the credit is not agreed
  ("not agreed", "asked", "proposed", "not confirmed") are demoted to
  "confirm" and labeled "Proposed, not agreed."
* Texas-only fields (option fee/days) are dropped for Colorado deals.
"""
import re
from datetime import datetime

import parser as _parser
import notepad as _notepad

PROMPT_VERSION = "deterministic-v1"
PROVIDER = "regex"

_HEDGE_RE = re.compile(
    r"\b(maybe|perhaps|around|about|approx\.?|approximately|ish|"
    r"not sure|probably|i think|supposedly)\b|\?", re.IGNORECASE)

# A seller credit the parties have not agreed to is a proposal, not a
# term (Accuracy Pass, 2026-11-11). The markers are checked against the
# concessions value, its span, and the clause around the span ("Asked
# 5k seller credit for the roof, not agreed yet" -- the "Asked" sits
# outside the extracted span but inside its clause).
_NOT_AGREED_RE = re.compile(
    r"\b(not\s+agreed|not\s+yet\s+agreed|not\s+confirmed|unconfirmed|"
    r"proposed|asked)\b", re.IGNORECASE)


def _not_agreed(text: str, span: str, value) -> bool:
    haystacks = [str(value or ""), span or ""]
    if span:
        start = text.find(span)
        if start >= 0:
            haystacks.append(_clause_around(text, start, start + len(span)))
    return any(_NOT_AGREED_RE.search(h) for h in haystacks)

_MONEY_STRICT = _notepad.STRICT_SPAN_FIELDS
_FREE_TEXT = _notepad.FREE_TEXT_FIELDS


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _clause_around(text: str, start: int, end: int, cap: int = 160) -> str:
    """The sentence-ish fragment containing text[start:end] -- used for
    hedge detection and free-text values. Commas count as boundaries:
    agent notes are comma-separated fragments, and a hedge three clauses
    away ("earnest maybe 7k ??, ..., signed Nov 10") must not taint a
    field it doesn't belong to. Returns an exact substring."""
    lo = start
    while lo > 0 and text[lo - 1] not in ".;\n," and start - lo < cap // 2:
        lo -= 1
    hi = end
    while hi < len(text) and text[hi] not in ".;\n," and hi - end < cap // 2:
        hi += 1
    return text[lo:hi]


# ---------------------------------------------------------------------------
# Span-aware extractors. Each returns (value, span) where span is an exact
# substring of the note, or None. Values mirror parser.py's semantics.
# ---------------------------------------------------------------------------

def _money(num_str: str, k_flag) -> int:
    val = float(str(num_str).replace(",", ""))
    if k_flag:
        val *= 1000 if str(k_flag).lower() == "k" else 1_000_000
    return int(round(val))


def _earnest_adjacent(text: str, start: int, end: int) -> bool:
    """True when a money match directly follows an earnest/option label
    ("earnest 7k") -- i.e. the match IS the earnest/option money. Only
    the text BEFORE the match counts: in "685k, earnest maybe 7k" the
    price is followed by an earnest clause and is still the price."""
    ctx = text[max(0, start - 14):start]
    return bool(re.search(r"earnest|option", ctx, re.IGNORECASE))


# Labeled-money patterns, shared by the field extractors below and by
# _find_price (which must not mistake labeled earnest/option money -- or
# a credit amount -- for the sales price).
_EARNEST_MONEY_PATTERNS = (
    r"earnest(?:\s+money)?\s*(?:of|is|:|at)?\s*\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?",
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*(?:in\s+|as\s+|for\s+)?earnest",
    r"earnest(?:\s+money)?\s*(?:of|is|:)?\s*(\d+(?:\.\d+)?)\s*(k)\b",
    r"(\d[\d,]*(?:\.\d+)?)\s*(k)?\s+earnest(?:\s+money)?\b",
    # Hedged: "earnest maybe 7k" -- a short same-clause gap is allowed,
    # but never across a comma (that would swallow the price clause).
    r"earnest(?:\s+money)?[^.,;\n]{0,20}?(\d+(?:\.\d+)?)\s*(k)\b",
)
_OPTION_FEE_PATTERNS = (
    r"option\s*(?:fee|money)\s*(?:of|is|:)?\s*\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?",
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*option\s*(?:fee|money)",
    r"\boption\s*\$\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?",
)

# Option/inspection day counts, plural-aware. parser.INSPECTION_RE only
# matches the singular ("10 day option"): a note's "10 days option" fell
# through to the generic day-count patterns and was read as close_days.
# parser.py is the SMS flow's module, so the wider pattern lives here.
_OPTION_DAYS_RE = re.compile(
    r"(\d+)[\s-]*days?\s*(?:option|inspection)(?:\s+period)?", re.IGNORECASE)


def _labeled_money_spans(text: str):
    spans = []
    for pat in _EARNEST_MONEY_PATTERNS + _OPTION_FEE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            spans.append(m.span())
    return spans


def _not_price_context(text: str, start: int) -> bool:
    """Amounts that follow words like "credit" are concessions/repairs,
    never the sales price ("roof credit 5k")."""
    ctx = text[max(0, start - 30):start]
    return bool(re.search(r"credit|concession|allowance|repair|toward",
                          ctx, re.IGNORECASE))


def _find_price(text: str):
    labeled = _labeled_money_spans(text)

    def excluded(m):
        if any(m.start() < e and s < m.end() for s, e in labeled):
            return True
        return _earnest_adjacent(text, m.start(), m.end()) \
            or _not_price_context(text, m.start())

    unit_re = re.compile(r"(\d+(?:\.\d+)?)\s*(k|m|million|mil)\b", re.IGNORECASE)
    matches = [m for m in unit_re.finditer(text) if not excluded(m)]
    values = []
    for m in matches:
        v = _money(m.group(1), m.group(2))
        if v not in values:
            values.append(v)
    if matches:
        first = matches[0]
        note = None
        if len(values) > 1:
            note = ("Your notes mention more than one price — "
                    "confirm the right one.")
        return _money(first.group(1), first.group(2)), text[first.start():first.end()], note
    for pat in (r"\b(\d{6,})\b", r"\b(\d{1,3}(?:,\d{3})+)\b"):
        for m in re.finditer(pat, text):
            v = int(m.group(1).replace(",", ""))
            if v >= 50_000 and not excluded(m):
                return v, m.group(0), None
    return None


def _find_down(text: str):
    """Down payment: percent forms first, then a dollar amount."""
    for pat in (r"(\d+(?:\.\d+)?)\s*(?:%|percent|pct)\s*(?:down|dn)\b",
                r"\bdown\s*(?:payment\s*)?(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:%|percent|pct)"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return "down_payment_pct", float(m.group(1)) / 100, m.group(0), None
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*(k)?\s*down\b", text, re.IGNORECASE)
    if m:
        return "down_payment_amount", _money(m.group(1), m.group(2)), m.group(0), None
    m = _parser.PCT_RE.search(text)
    if m and not _earnest_adjacent(text, m.start(), m.end()) \
            and not re.search(r"earnest|option", text[m.end():m.end() + 12],
                              re.IGNORECASE):
        return ("down_payment_pct", float(m.group(1)) / 100, m.group(0),
                "A percentage in your notes — confirm it's the down payment.")
    return None


def _iso_from_month_day(month: int, day: int):
    """Month/day from a note, resolved against today. A date in the
    recent past (<= 45 days -- "signed Nov 10" written on Nov 12) stays
    this year; only a date long past rolls to next year, where a bare
    month/day in a note almost always means the upcoming occurrence."""
    today = datetime.now().date()
    try:
        target = datetime(today.year, month, day).date()
    except ValueError:
        return None
    if (today - target).days > 45:
        try:
            target = datetime(today.year + 1, month, day).date()
        except ValueError:
            return None
    return target


def _find_closing(text: str):
    """Returns (fields_dict_fragment): closing_date and/or close_days."""
    out = {}
    m = _parser.CLOSE_DATE_RE.search(text)
    if m:
        target = _iso_from_month_day(_parser.MONTH_NAMES[m.group(1).lower()],
                                     int(m.group(2)))
        if target:
            out["closing_date"] = (target.isoformat(), m.group(0), None)
            # close_days is NOT extracted from a stated date: it is a
            # subtraction (date - today), so it belongs to the sheet's
            # Calculated layer, derived once the date is Captured
            # (notepad.refresh_calculated_fields).
            return out
    m = re.search(r"\bclos(?:e|ing)\s+(?:on\s+)?(20\d\d-\d\d-\d\d)\b", text, re.IGNORECASE)
    if m:
        out["closing_date"] = (m.group(1), m.group(0), None)
        return out
    # Date-first phrasing: "Dec 18 close" / "Sellers fine with Dec 18
    # close" -- the month/day BEFORE the word close, which CLOSE_DATE_RE
    # (close-first only) can't see. The date only: close_days is not
    # derived from it, so a note whose only day count is the option
    # period doesn't gain a phantom close_days from this branch either.
    m = re.search(r"\b(" + "|".join(_parser.MONTH_NAMES.keys()) +
                  r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s+clos(?:e|ing)\b",
                  text, re.IGNORECASE)
    if m:
        target = _iso_from_month_day(_parser.MONTH_NAMES[m.group(1).lower()],
                                     int(m.group(2)))
        if target:
            out["closing_date"] = (target.isoformat(), m.group(0), None)
            return out
    insp = _OPTION_DAYS_RE.search(text)
    insp_span = insp.span() if insp else None
    for m in _parser.CLOSE_PHRASE_RE.finditer(text):
        out["close_days"] = (int(m.group(1)), m.group(0), None)
        return out
    m = re.search(r"(\d+)[\s-]*day\s*clos", text, re.IGNORECASE)
    if m:
        out["close_days"] = (int(m.group(1)), m.group(0), None)
        return out
    for m in _parser.DAYS_RE.finditer(text):
        if insp_span and m.start() >= insp_span[0] and m.end() <= insp_span[1]:
            continue  # the option/inspection count, not the close
        out["close_days"] = (int(m.group(1)), m.group(0), None)
        return out
    return out


def _find_dated(text: str, label_pat: str):
    m = re.search(label_pat + r"\s*(?:on\s+)?(" +
                  "|".join(_parser.MONTH_NAMES.keys()) +
                  r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text, re.IGNORECASE)
    if m:
        target = _iso_from_month_day(_parser.MONTH_NAMES[m.group(1).lower()],
                                     int(m.group(2)))
        if target:
            return target.isoformat(), m.group(0)
    return None


def _find_labeled_money(text: str, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return _money(m.group(1), m.group(2) if m.lastindex and m.lastindex >= 2 else None), m.group(0)
    return None


def _find_party(text: str, role: str):
    m = re.search(rf"\b{role}s?\s*[:\-]\s*([^\n,;.]+)", text, re.IGNORECASE)
    if m:
        value = re.sub(r"\s+(?:wants|agreed|needs|will|is asking).*$", "",
                       m.group(1)).strip(" -")
        if 2 <= len(value) <= 60:
            start = m.start(1)
            return value, text[start:start + len(value)]
    # The role word itself is case-insensitive (scoped (?i:...)): a note
    # that starts a sentence with "Buyers John & Jane Doe" must match
    # just like mid-sentence "buyers ...". The NAME stays case-sensitive
    # -- it must be capitalized in the note to be proposed at all.
    m = re.search(rf"\b(?i:{role}s?)\s+(?:is|are|named)\s+"
                  rf"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){{0,2}})", text)
    if m:
        name = m.group(1).rstrip(".")
        return name, name
    # Bare form: "buyers John & Jane Doe" -- capitalized name(s) right
    # after the role word ("buyers agent" can't match: lowercase). The
    # name class allows periods for initials ("John Q. Doe"), so a
    # sentence-ending period gets trimmed back off the capture.
    m = re.search(rf"\b(?i:{role}s?)\s+([A-Z][A-Za-z.'-]+"
                  rf"(?:\s+(?:&|and)\s+[A-Z][A-Za-z.'-]+)?"
                  rf"(?:\s+[A-Z][A-Za-z.'-]+)?)", text)
    if m:
        name = m.group(1).rstrip(".")
        return name, name
    return None


def _find_title_escrow(text: str):
    out = {}
    m = re.search(r"\btitle\s*[:\-]\s*([^\n,;.]+)", text, re.IGNORECASE)
    if m and 2 <= len(m.group(1).strip()) <= 60:
        out["title_company"] = (m.group(1).strip(), m.group(1).strip())
    else:
        m = re.search(r"\btitle\s+(?:company\s+)?(?:is|with|at)\s+"
                      r"([A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,3})", text)
        if m:
            out["title_company"] = (m.group(1), m.group(1))
    m = re.search(r"\bescrow\s*(?:agent\s*)?[:\-]\s*([^\n,;.]+)", text, re.IGNORECASE)
    if m and 2 <= len(m.group(1).strip()) <= 60:
        out["escrow_agent_name"] = (m.group(1).strip(), m.group(1).strip())
    else:
        m = re.search(r"\bescrow\s+(?:agent\s+)?(?:is|with|at)\s+"
                      r"([A-Z][\w&.'-]+(?:\s+[A-Z][\w&.'-]+){0,3})", text)
        if m:
            out["escrow_agent_name"] = (m.group(1), m.group(1))
    return out


def _clause_field(text: str, pattern: str, require: str = None):
    """Free-text field from the clause matching `pattern` (optionally also
    requiring `require` inside the clause)."""
    for m in re.finditer(r"[^.;\n,]+", text):
        clause = m.group(0)
        if re.search(pattern, clause, re.IGNORECASE) and \
                (require is None or re.search(require, clause, re.IGNORECASE)):
            value = clause.strip(" ,-")
            if 3 <= len(value) <= 200:
                start = m.start() + (len(clause) - len(clause.lstrip()))
                return value, text[start:start + len(value)]
    return None


# Words that end an address when they carry the sentence period
# ("123 Main St." -- the period belongs to the address, not the cut).
_SUFFIX_WORDS = frozenset(
    "st street ave avenue blvd boulevard dr drive ln lane ct court rd "
    "road way pkwy parkway pl place cir circle trl trail loop pass run "
    "cv cove hwy highway".split())

# Signals that the note has moved past the address into another field.
_ADDRESS_SIGNAL_RE = re.compile(
    r"\b(?:buyers?|sellers?|earnest|option|financing|hoa|survey|fridge|"
    r"washer|credit|conventional|fha|usda)\b|\$", re.IGNORECASE)


def _bounded_address(text: str, street) -> str:
    """The address portion of the note, starting at the street match and
    cut at the first sentence boundary or field signal. parser's
    _parse_address is built for one-line SMS shorthand: on prose notes
    it strips the field tokens and returns everything left, so the
    "address" swallowed whole sentences ("123 Main St ... Buyers John &
    Jane Doe. Sellers fine with ..."). Bounding the raw substring here
    keeps the proposal to the address itself -- and span == value, an
    exact substring by construction."""
    start = street.start()
    end = street.end()
    limit = min(len(text), start + 120)
    cut = limit
    for m in re.finditer(r"\.", text[end:limit]):
        dot = end + m.start()
        word = re.search(r"([A-Za-z]+)$", text[start:dot])
        if word and word.group(1).lower() in _SUFFIX_WORDS:
            continue  # "St." / "Dr." -- the period is part of the address
        cut = dot
        break
    sig = _ADDRESS_SIGNAL_RE.search(text, end, limit)
    if sig:
        cut = min(cut, sig.start())
    return text[start:cut].strip(" ,.") or None


def _extract_fields(raw_text: str) -> dict:
    """Deterministic extraction. Returns {key: (value, span, note)}."""
    text = raw_text or ""
    found = {}

    hit = _find_price(text)
    if hit:
        found["price"] = hit
    hit = _find_down(text)
    if hit:
        key, value, span, note = hit
        found[key] = (value, span, note)
    for key, hit in _find_closing(text).items():
        found[key] = hit
    m = _parser.FINANCING_RE.search(text)
    if m:
        found["financing_type"] = (
            _parser._normalize_financing_type(m.group(1)), m.group(0), None)
    hit = _find_dated(text, r"(?:effective|signed|executed)")
    if hit:
        found["effective_date"] = (hit[0], hit[1], None)
    m = _OPTION_DAYS_RE.search(text)
    if m:
        found["option_days"] = (int(m.group(1)), m.group(0), None)
    hit = _find_labeled_money(text, _EARNEST_MONEY_PATTERNS)
    if hit:
        found["earnest_money_amount"] = (hit[0], hit[1], None)
    m = re.search(r"earnest(?:\s+money)?\s*(?:of|is|:)?\s*(\d+(?:\.\d+)?)\s*(?:%|percent)"
                  r"|(\d+(?:\.\d+)?)\s*(?:%|percent)\s*earnest", text, re.IGNORECASE)
    if m:
        pct = float(m.group(1) or m.group(2)) / 100
        found["earnest_pct"] = (pct, m.group(0), None)
    hit = _find_labeled_money(text, _OPTION_FEE_PATTERNS)
    if hit:
        found["option_fee_amount"] = (hit[0], hit[1], None)

    parsed_address = _parser._parse_address(text)
    street = re.search(
        r"\b\d{1,6}\s+[A-Za-z0-9'’ .\-]+?\s+(?:" + _parser.STREET_SUFFIXES + r")\b\.?",
        text, re.IGNORECASE)
    if parsed_address and street:
        bounded = _bounded_address(text, street)
        if bounded:
            found["address"] = (bounded, bounded, None)
    city = _parser._parse_city(text)
    if city:
        for m in re.finditer(re.escape(city), text, re.IGNORECASE):
            after = text[m.end():m.end() + 16]
            if not re.match(r"\s+(?:" + _parser.STREET_SUFFIXES + r")\b",
                            after, re.IGNORECASE):
                found["city"] = (city, m.group(0), None)
                break
    county = _parser._parse_county(text, parsed_address, city)
    if county:
        m = re.search(re.escape(county), text, re.IGNORECASE)
        if m:
            found["county"] = (county, m.group(0), None)
    for role, key in (("buyer", "buyer_name"), ("seller", "seller_name")):
        hit = _find_party(text, role)
        if hit:
            found[key] = (hit[0], hit[1], None)
    for key, hit in _find_title_escrow(text).items():
        found[key] = (hit[0], hit[1], None)
    if _parser._parse_has_hoa(text):
        hit = _clause_field(text, r"\bhoa\b")
        if hit:
            found["hoa"] = (hit[0], hit[1], None)
    hit = _clause_field(text, r"fridge|refrigerator|washer|dryer|appliances",
                        require=r"stay|stays|convey|include|remain")
    if hit:
        found["inclusions"] = (hit[0], hit[1], None)
    hit = _clause_field(text, r"contingenc|subject to|appraisal")
    if hit:
        found["contingencies"] = (hit[0], hit[1], None)
    m = re.search(r"\bseller(?:'s)?\s+(?:will\s+)?(?:pay|pays|credit|credits|"
                  r"contribute|cover|covers)\b[^.;\n]{0,70}", text, re.IGNORECASE)
    if m:
        found["seller_concessions"] = (m.group(0).strip(), m.group(0), None)
    return found


def enhance_note(raw_text: str, state: str = "TX", profile: dict = None) -> dict:
    """Deterministic enhancement of one raw note. Same return shape the
    future LLM producer will use:

        {"status", "summary", "fields", "action_items", "warnings",
         "provider", "model", "prompt_version", "input_tokens",
         "output_tokens"}

    Every proposed field is state "confirm" with origin "regex" and an
    exact source span. The summary is empty -- a regex cannot summarize,
    and an empty summary is the honest output (the UI says so)."""
    fields = {}
    warnings = []
    for key, (value, span, note) in _extract_fields(raw_text).items():
        field = {"value": value, "state": "confirm", "span": span,
                 "origin": "regex", "note": ""}
        hedge_src = _clause_around(raw_text, raw_text.find(span), raw_text.find(span) + len(span)) \
            if span else ""
        if note:
            field["note"] = note
            if "more than one price" in note:
                warnings.append(note)
        elif hedge_src and _HEDGE_RE.search(hedge_src):
            field["note"] = "Your notes hedge this one — confirm the exact value."
        if key == "seller_concessions" and _not_agreed(raw_text, span, value):
            # The words say this credit is not agreed: label it as the
            # proposal it is (wins over the generic hedge note).
            field["note"] = "Proposed, not agreed."
        fields[key] = field
    return {
        "status": "fallback_regex",
        "summary": "",
        "fields": fields,
        "action_items": [],
        "warnings": warnings,
        "provider": PROVIDER,
        "model": "",
        "prompt_version": PROMPT_VERSION,
        "input_tokens": 0,
        "output_tokens": 0,
    }


# ---------------------------------------------------------------------------
# Validation layer -- the no-invention enforcement (spec 4.4)
# ---------------------------------------------------------------------------

def _demote(field: dict) -> dict:
    field = dict(field)
    field["state"] = "confirm"
    return field


def validate_proposal(raw_text: str, proposal_fields: dict, state: str = "TX"):
    """Gate any producer's proposal before it may touch a deal sheet.
    Returns (fields, warnings). See the module docstring for the rules."""
    norm_note = _norm(raw_text)
    out = {}
    warnings = []
    for key, field in (proposal_fields or {}).items():
        if key not in _notepad.FIELD_KEYS:
            continue  # unknown keys are dropped silently
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if value is None or value == "":
            continue
        field = dict(field)
        if field.get("state") not in ("found", "confirm"):
            field["state"] = "confirm"
        field.setdefault("note", "")
        field.setdefault("origin", "")
        span = field.get("span") or ""
        span_ok = bool(span) and _norm(span) in norm_note
        if not span_ok:
            if key in _FREE_TEXT and _norm(value) in norm_note:
                field = _demote(field)
                field["note"] = field.get("note") or \
                    "Couldn't pin this to one place in your notes — confirm it."
            else:
                warnings.append(
                    f"Dropped {_notepad.FIELD_LABELS.get(key, key)}: "
                    "it couldn't be verified against your notes.")
                continue
        # Type / range checks: demote, never silently fix.
        kind = _notepad.FIELD_KINDS.get(key)
        try:
            if kind == "money":
                v = float(str(value).replace(",", "").replace("$", ""))
                if not (0 <= v <= 10_000_000) or (key == "price" and v < 1):
                    field = _demote(field)
                    warnings.append(
                        f"{_notepad.FIELD_LABELS.get(key, key)} looks out of "
                        "range — confirm the amount.")
            elif kind == "pct":
                v = float(value)
                if not (0 < v <= 1):
                    field = _demote(field)
                    warnings.append(
                        f"{_notepad.FIELD_LABELS.get(key, key)} isn't a "
                        "usable percentage — confirm it.")
            elif kind == "int":
                v = int(float(value))
                lo, hi = (7, 365) if key == "close_days" else (1, 60)
                if not (lo <= v <= hi):
                    field = _demote(field)
                    warnings.append(
                        f"{_notepad.FIELD_LABELS.get(key, key)} is outside "
                        f"the usual {lo}–{hi} range — confirm it.")
            elif kind == "date":
                datetime.strptime(str(value), "%Y-%m-%d")
        except (TypeError, ValueError):
            field = _demote(field)
            if kind in ("money", "pct", "int", "date"):
                warnings.append(
                    f"{_notepad.FIELD_LABELS.get(key, key)} couldn't be "
                    "read as a usable value — confirm it.")
        if key == "seller_concessions" and _not_agreed(raw_text, span, value):
            # Producer-agnostic: a credit whose own words say it is not
            # agreed can never be firmer than Confirm, and is labeled
            # as the proposal it is.
            field = _demote(field)
            field["note"] = "Proposed, not agreed."
        out[key] = field

    if state == "CO":
        for key in _notepad.TX_ONLY_FIELDS:
            out.pop(key, None)
    eff, close = out.get("effective_date"), out.get("closing_date")
    if eff and close:
        try:
            if str(close["value"]) < str(eff["value"]):
                out["effective_date"] = _demote(eff)
                out["closing_date"] = _demote(close)
                warnings.append(
                    "The closing date is before the effective date — "
                    "confirm both dates.")
        except TypeError:
            pass
    return out, warnings
