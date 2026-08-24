"""
parser.py — turns a freeform SMS like:
 "725k 3% 21day 123 Main St"
into structured offer data.
No LLM call needed for the common patterns. Falls back to
returning an 'error' key with a hint message if it can't parse.
"""
import re
from datetime import datetime

PRICE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([kK]|million|mil|m\b)?')
PCT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:%|percent|pct)', re.IGNORECASE)
DAYS_RE = re.compile(r'(\d+)\s*(?:day|days)', re.IGNORECASE)
CLOSE_PHRASE_RE = re.compile(r'close\s+(?:in\s+)?(\d+)\s*(?:day|days)?', re.IGNORECASE)

# Inspection/option period ("10-day inspection", "10 day option period") --
# stripped out before the generic day-count patterns above run, so a message
# combining both a closing timeframe and an inspection period doesn't have
# the inspection number mistaken for the closing days.
INSPECTION_RE = re.compile(r'(\d+)[\s-]*day\s*(?:inspection|option)', re.IGNORECASE)

FINANCING_RE = re.compile(r'\b(conventional|fha|va|cash)\b', re.IGNORECASE)
HOA_RE = re.compile(r'\bhoa\b', re.IGNORECASE)

MONTH_NAMES = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9, 'oct': 10,
    'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}
# "close Sept 15", "closing September 15th" -- an absolute date instead of
# the relative "21day" shorthand. Converted to a day count from today.
CLOSE_DATE_RE = re.compile(
    r'clos(?:e|ing)\s+(?:on\s+)?(' + '|'.join(MONTH_NAMES.keys()) + r')\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b',
    re.IGNORECASE
)
# Common TX counties - agents can specify to avoid geocoding lookup
TX_COUNTIES = [
    'travis', 'harris', 'bexar', 'dallas', 'tarrant', 'collin', 'denton',
    'williamson', 'fort bend', 'montgomery', 'el paso', 'hidalgo', 'cameron',
]
COUNTY_RE = re.compile(r'\b(' + '|'.join(TX_COUNTIES) + r')\b', re.IGNORECASE)

def _parse_price(text):
    # Priority 1: number with explicit unit (k/m/million)
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(k|m|million|mil)\b', text, re.IGNORECASE):
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit == 'k':
            return int(num * 1_000)
        else:
            return int(num * 1_000_000)
    # Priority 2: bare number > 50,000 (likely a price, not days or pct)
    for m in re.finditer(r'\b(\d{6,})\b', text):
        num = int(m.group(1))
        if num >= 50_000:
            return num
    # Priority 3: number with comma formatting (e.g. 725,000)
    for m in re.finditer(r'\b(\d{1,3}(?:,\d{3})+)\b', text):
        num = int(m.group(1).replace(',', ''))
        if num >= 50_000:
            return num
    return None

def _parse_pct(text):
    # "3%", "3 percent", "3 pct"
    m = PCT_RE.search(text)
    if m:
        return float(m.group(1)) / 100
    # "3 down" or "3down"
    m = re.search(r'(\d+(?:\.\d+)?)\s*down', text, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if val <= 50:
            return val / 100
    return None

def _parse_days(text, today=None):
    # Absolute date ("close Sept 15") takes priority when present -- convert
    # to a day count from today so the rest of the app (which works entirely
    # in close_days) doesn't need to change.
    m = CLOSE_DATE_RE.search(text)
    if m:
        month = MONTH_NAMES[m.group(1).lower()]
        day = int(m.group(2))
        today = today or datetime.now()
        try:
            target = datetime(today.year, month, day)
        except ValueError:
            target = None
        if target is not None:
            if target.date() < today.date():
                target = datetime(today.year + 1, month, day)
            return (target.date() - today.date()).days

    # Strip inspection/option-period phrases first so a message that mentions
    # both a closing timeframe and an inspection period ("21day ... 10-day
    # inspection") doesn't have the inspection number mistaken for close_days.
    stripped = INSPECTION_RE.sub('', text)

    # "21day", "21 days", "21days"
    m = DAYS_RE.search(stripped)
    if m:
        return int(m.group(1))
    # "close in 21", "close in 21 days"
    m = CLOSE_PHRASE_RE.search(stripped)
    if m:
        return int(m.group(1))
    # "21 day close" or "21-day close"
    m = re.search(r'(\d+)[\s-]*day\s*clos', stripped, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None

def _parse_inspection_days(text):
    # "10-day inspection", "10 day option", "10day option period"
    m = INSPECTION_RE.search(text)
    return int(m.group(1)) if m else None

def _parse_financing_type(text):
    # "conventional", "FHA", "VA", "cash" -- optional; left unset (agent
    # fills it in) rather than guessed, same policy as every other field
    # this parser doesn't have explicit evidence for.
    m = FINANCING_RE.search(text)
    return m.group(1).lower() if m else None

def _parse_has_hoa(text):
    # "hoa" mentioned anywhere in the text -- deliberate, explicit signal
    # only. Not every property has a mandatory HOA, so unlike financing
    # type/inspection days (which just stay unset if absent), this one
    # controls whether an entire addendum gets attached -- no attempt to
    # infer it from MLS/property data, only the agent's own words.
    return bool(HOA_RE.search(text))


def _parse_county(text):
    # Look for county name in the text
    m = COUNTY_RE.search(text)
    return m.group(1).title() if m else None

def _parse_city(text):
    # Common TX cities - look for them in the text
    # This is a heuristic; real version should use geocoding
    cities = [
        'austin', 'houston', 'san antonio', 'dallas', 'fort worth', 'el paso',
        'arlington', 'corpus christi', 'plano', 'irving', 'laredo', 'garland',
        'frisco', 'mckinney', 'round rock', 'cedar park', 'pflugerville', 'georgetown'
    ]
    text_lower = text.lower()
    for city in cities:
        if city in text_lower:
            return city.title()
    return None

STREET_SUFFIXES = r'(?:st|street|ave|avenue|blvd|boulevard|dr|drive|ln|lane|ct|court|rd|road|way|pkwy|parkway|pl|place|cir|circle|trl|trail|loop|pass|run|cv|cove|hwy|highway)'

# Words that get a fixed capitalization instead of generic title-case, keyed
# lowercase -> display form (street suffixes + directionals + state abbrev).
_ADDRESS_FIXED_CASE = {
    'st': 'St', 'street': 'Street', 'ave': 'Ave', 'avenue': 'Avenue',
    'blvd': 'Blvd', 'boulevard': 'Boulevard', 'dr': 'Dr', 'drive': 'Drive',
    'ln': 'Ln', 'lane': 'Lane', 'ct': 'Ct', 'court': 'Court', 'rd': 'Rd',
    'road': 'Road', 'way': 'Way', 'pkwy': 'Pkwy', 'parkway': 'Parkway',
    'pl': 'Pl', 'place': 'Place', 'cir': 'Cir', 'circle': 'Circle',
    'trl': 'Trl', 'trail': 'Trail', 'loop': 'Loop', 'pass': 'Pass',
    'run': 'Run', 'cv': 'Cv', 'cove': 'Cove', 'hwy': 'Hwy', 'highway': 'Highway',
    'tx': 'TX', 'texas': 'Texas',
    'n': 'N', 's': 'S', 'e': 'E', 'w': 'W', 'ne': 'NE', 'nw': 'NW', 'se': 'SE', 'sw': 'SW',
}


def _titlecase_address(addr: str) -> str:
    """Title-case a street address, keeping suffixes/directionals in their
    conventional form (e.g. "atlantic ave" -> "Atlantic Ave", not "Atlantic
    Ave." or "ATLANTIC AVE"). Preserves trailing punctuation on each word."""
    words = addr.split()
    out = []
    for w in words:
        core = w.strip('.,')
        trail = w[len(core):]
        key = core.lower()
        if key in _ADDRESS_FIXED_CASE:
            out.append(_ADDRESS_FIXED_CASE[key] + trail)
        elif core.isdigit():
            out.append(w)
        else:
            out.append((core[:1].upper() + core[1:].lower() if core else core) + trail)
    return " ".join(out)

def _parse_address(text):
    # Strip out the price/pct/day tokens, what's left is the street address
    # Only strip number+unit combos (not bare "million" in street names like "100 Million Dr")
    stripped = re.sub(r'\d+(?:\.\d+)?\s*(k|m|million|mil)\b(?!\s+' + STREET_SUFFIXES + r'\b)', '', text, flags=re.IGNORECASE)
    stripped = re.sub(r'\b\d{6,}\b', '', stripped)  # bare large numbers (price)
    stripped = re.sub(r'\b\d{1,3}(?:,\d{3})+\b', '', stripped)  # comma-formatted numbers
    stripped = re.sub(r'\d+(?:\.\d+)?\s*(?:%|percent|pct)', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\d+(?:\.\d+)?\s*down\b', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\d+[\s-]*day\w*', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\bclose\s+(?:in\s+)?\d+\s*(?:day|days)?\b', '', stripped, flags=re.IGNORECASE)
    stripped = CLOSE_DATE_RE.sub('', stripped)
    stripped = re.sub(r'\b(?:offer|down|closing|percent|pct|inspection|option|conventional|fha|va|cash|hoa)\b', '', stripped, flags=re.IGNORECASE)
    # Remove county/city names only when NOT followed by a street suffix
    # (protects addresses like "123 Dallas Pkwy" or "456 El Paso Dr")
    all_place_names = list(TX_COUNTIES) + [
        'austin', 'houston', 'san antonio', 'dallas', 'fort worth', 'el paso',
        'arlington', 'corpus christi', 'plano', 'irving', 'laredo', 'garland',
        'frisco', 'mckinney', 'round rock', 'cedar park', 'pflugerville', 'georgetown']
    # Deduplicate and sort longest first so multi-word names match before single-word
    all_place_names = sorted(set(all_place_names), key=len, reverse=True)
    for name in all_place_names:
        pattern = r'\b' + name + r'\b(?!\s+' + STREET_SUFFIXES + r'\b)'
        stripped = re.sub(pattern, '', stripped, flags=re.IGNORECASE)
    # Remove trailing state abbreviation (TX, Texas) and TX zip codes (7xxxx only)
    stripped = re.sub(r'\b(?:TX|Texas)\b', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\b7\d{4}(?:-\d{4})?\b', '', stripped)
    address = re.sub(r'\s+', ' ', stripped).strip(' ,.-')
    # Collapse an accidental duplicate street suffix ("Ocean Blvd Ave" ->
    # "Ocean Blvd") -- two suffix words back-to-back is never a real US
    # street name, just a stray extra word. Keeps the first: it sits
    # immediately after the actual street name, so it's the one that
    # matters if only one can be kept.
    address = re.sub(
        r'\b(' + STREET_SUFFIXES + r')\.?\s+' + STREET_SUFFIXES + r'\.?\b',
        r'\1', address, flags=re.IGNORECASE
    )
    return _titlecase_address(address) if address else None

def parse_offer_sms(text: str) -> dict:
    """
    Returns:
    {
        "price": int,
        "down_payment_pct": float, # e.g. 0.03
        "close_days": int,
        "address": str,
        "county": str (optional)
    }
    or:
    {"error": "explanation", "raw_text": text}
    """
    text = text.strip()
    price = _parse_price(text)
    financing_type = _parse_financing_type(text)  # optional
    pct = _parse_pct(text)
    # "cash" with no explicit percent means 100% down / no financing -- an
    # all-cash offer never has a separate down-payment figure to state.
    # An explicit percent alongside "cash" (unusual, but not this parser's
    # place to reject as a typo) still wins; only fill the gap when nothing
    # else specified a percentage.
    if pct is None and financing_type == "cash":
        pct = 1.0
    days = _parse_days(text)
    county = _parse_county(text)  # optional
    city = _parse_city(text)  # optional
    address = _parse_address(text)
    inspection_days = _parse_inspection_days(text)  # optional

    # Field-specific error message ("Variant B") instead of a raw list of
    # missing field names -- names one concrete thing to fix rather than
    # making the agent cross-reference a list against their own text, which
    # is a meaningfully lower cognitive load when someone's texting
    # one-handed. Priority order below matches the natural reading order of
    # the format template itself (price, then %, then days, then address),
    # so when multiple fields are missing the earliest one in that order is
    # the one called out.
    FIELD_HINTS = [
        ("price", price, "Couldn't find a price. Try a number like 725k."),
        ("down_payment_pct", pct, "Couldn't find a down payment %. Try something like 3%."),
        ("close_days", days, "Couldn't find a closing timeframe. Try something like 21day."),
        ("address", address, "Couldn't find an address. Include street number, name, and type."),
    ]
    first_missing = next((hint for _, val, hint in FIELD_HINTS if val is None), None)

    if first_missing:
        return {
            "error": f"{first_missing} Full format: 725k 3% 21day Travis 123 Main St",
            "raw_text": text
        }

    # Validate ranges
    if price <= 0:
        return {
            "error": f"Price must be greater than $0 (got ${price:,})",
            "raw_text": text
        }
    if price > 50_000_000:
        return {
            "error": f"Price ${price:,} seems too high. Max $50M for residential TX real estate.",
            "raw_text": text
        }

    if pct <= 0:
        return {
            "error": f"Down payment must be greater than 0% (got {pct*100:.1f}%)",
            "raw_text": text
        }
    if pct > 0.5 and financing_type != "cash":
        return {
            "error": f"Down payment {pct*100:.1f}% seems too high. Max 50% for typical offers.",
            "raw_text": text
        }

    if days < 7:
        return {
            "error": f"Closing in {days} days is too fast. Minimum 7 days.",
            "raw_text": text
        }
    if days > 365:
        return {
            "error": f"Closing in {days} days is too far out. Maximum 365 days.",
            "raw_text": text
        }

    # Validate address has meaningful content (more than just 1-2 characters)
    if len(address.strip()) < 5:
        return {
            "error": f'Address "{address}" is too short. Include street number, name, and type (e.g., 123 Main St)',
            "raw_text": text
        }

    result = {
        "price": price,
        "down_payment_pct": pct,
        "close_days": days,
        "address": address
    }

    # Add county and city if specified (optional)
    if county:
        result["county"] = county
    if city:
        result["city"] = city
    if financing_type:
        result["financing_type"] = financing_type
    if inspection_days is not None:
        result["inspection_days"] = inspection_days
    if _parse_has_hoa(text):
        result["has_hoa"] = True

    return result

def parse_correction_sms(text: str) -> dict:
    """
    Extracts whichever offer fields are actually present in a short
    correction reply to a pending confirmation, e.g. "make it 820k" or
    "close in 25 days" or "FHA instead". Unlike parse_offer_sms, does NOT
    require every field -- the caller merges whatever's returned into the
    already-pending draft rather than starting one from scratch.
    Returns a dict with only the keys actually found (possibly empty).
    """
    text = text.strip()
    result = {}
    price = _parse_price(text)
    if price is not None:
        result["price"] = price
    financing_type = _parse_financing_type(text)
    pct = _parse_pct(text)
    if pct is None and financing_type == "cash":
        pct = 1.0  # see parse_offer_sms -- "cash instead" implies 100% down
    if pct is not None:
        result["down_payment_pct"] = pct
    days = _parse_days(text)
    if days is not None:
        result["close_days"] = days
    if financing_type:
        result["financing_type"] = financing_type
    inspection_days = _parse_inspection_days(text)
    if inspection_days is not None:
        result["inspection_days"] = inspection_days
    if _parse_has_hoa(text):
        result["has_hoa"] = True
    return result


AMEND_PRICE_RE = re.compile(r'\bprice\s+\$?(\d+(?:\.\d+)?)\s*(k|m|million|mil)?\b', re.IGNORECASE)
AMEND_CLOSE_RE = re.compile(r'\bclose\s+\+\s*(\d+)\b', re.IGNORECASE)


def parse_amendment_sms(text: str) -> dict:
    """
    Parses "AMEND <address> price <value>" or "AMEND <address> close +<days>".
    Only one field per amendment -- keeps each generated 39-11 to a single,
    unambiguous change instead of guessing at combined edits.
    Returns {"address": str, "field": "price"|"close", "value": int} or {"error": str}.
    """
    text = re.sub(r'^AMEND\s+', '', text.strip(), flags=re.IGNORECASE)

    price_m = AMEND_PRICE_RE.search(text)
    close_m = AMEND_CLOSE_RE.search(text)

    if price_m and close_m:
        return {"error": "Amend one thing at a time -- price OR close, not both. Send two separate AMEND texts."}

    if not price_m and not close_m:
        return {"error": "Amend format: AMEND <address> price <value>  OR  AMEND <address> close +<days>\n"
                          "Example: AMEND 123 Main St price 730k"}

    if price_m:
        num = float(price_m.group(1))
        unit = (price_m.group(2) or '').lower()
        if unit == 'k':
            value = int(num * 1_000)
        elif unit in ('m', 'million', 'mil'):
            value = int(num * 1_000_000)
        else:
            value = int(num)
        if value <= 0 or value > 50_000_000:
            return {"error": f"Amended price ${value:,} is out of range."}
        field = "price"
        address = text[:price_m.start()].strip(' ,.-')
    else:
        value = int(close_m.group(1))
        if value <= 0 or value > 180:
            return {"error": f"Amended closing extension of {value} days is out of range (max 180)."}
        field = "close"
        address = text[:close_m.start()].strip(' ,.-')

    if len(address) < 5:
        return {"error": "Could not find a property address. Format: AMEND <address> price <value>  OR  AMEND <address> close +<days>"}

    return {"address": address, "field": field, "value": value}


if __name__ == "__main__":
    tests = [
        # Original format
        ("725k 3% 21day 123 Main St", 725000, 0.03, 21),
        ("725k 3% 21day Travis 123 Main St", 725000, 0.03, 21),
        ("650k 3% 30day Harris 123 Main St Houston", 650000, 0.03, 30),
        ("1.2m 10% 45days Bexar 500 Ocean Blvd San Antonio", 1200000, 0.10, 45),
        # Messy real-world inputs
        ("725000 3% 21day 123 Main St", 725000, 0.03, 21),
        ("Offer 725k, 3 percent down, close in 21 days, 123 Main St Austin", 725000, 0.03, 21),
        ("725k 3 down 21day 123 Main St", 725000, 0.03, 21),
        ("725,000 3% 21day 123 Main St", 725000, 0.03, 21),
        ("725k 3% 21 day close 123 Main St", 725000, 0.03, 21),
        ("123 Main St Austin, 725k, 3%, 21 days", 725000, 0.03, 21),
        ("725k 10% 30day 123 Main St", 725000, 0.10, 30),
        ("725000 3 percent 21 days 123 Main St, Austin TX 78701", 725000, 0.03, 21),
        # All-cash offer -- "cash" with no explicit percent implies 100% down
        ("725k cash 21day 123 Main St", 725000, 1.0, 21),
    ]
    passed = 0
    failed = 0
    for test in tests:
        text = test[0]
        expected_price, expected_pct, expected_days = test[1], test[2], test[3]
        result = parse_offer_sms(text)
        if "error" in result:
            print(f"FAIL: {text}\n      Error: {result['error']}\n")
            failed += 1
        elif result["price"] != expected_price or abs(result["down_payment_pct"] - expected_pct) > 0.001 or result["close_days"] != expected_days:
            print(f"FAIL: {text}\n      Got: price={result['price']}, pct={result['down_payment_pct']}, days={result['close_days']}\n      Expected: price={expected_price}, pct={expected_pct}, days={expected_days}\n")
            failed += 1
        else:
            print(f"PASS: {text}")
            passed += 1
    print(f"\n{passed}/{passed+failed} tests passed")

