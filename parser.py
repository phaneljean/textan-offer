"""
parser.py — turns a freeform SMS like:
 "725k 3% 21day 1740 Grand Ave"
into structured offer data.
No LLM call needed for the common patterns. Falls back to
returning an 'error' key with a hint message if it can't parse.
"""
import re

PRICE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([kK]|million|mil|m\b)?')
PCT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(?:%|percent|pct)', re.IGNORECASE)
DAYS_RE = re.compile(r'(\d+)\s*(?:day|days)', re.IGNORECASE)
CLOSE_PHRASE_RE = re.compile(r'close\s+(?:in\s+)?(\d+)\s*(?:day|days)?', re.IGNORECASE)
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

def _parse_days(text):
    # "21day", "21 days", "21days"
    m = DAYS_RE.search(text)
    if m:
        return int(m.group(1))
    # "close in 21", "close in 21 days"
    m = CLOSE_PHRASE_RE.search(text)
    if m:
        return int(m.group(1))
    # "21 day close" or "21-day close"
    m = re.search(r'(\d+)[\s-]*day\s*clos', text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None

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
    stripped = re.sub(r'\b(?:offer|down|closing|percent|pct)\b', '', stripped, flags=re.IGNORECASE)
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
    return address if address else None

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
    pct = _parse_pct(text)
    days = _parse_days(text)
    county = _parse_county(text)  # optional
    city = _parse_city(text)  # optional
    address = _parse_address(text)

    missing = [name for name, val in
        [("price", price), ("down_payment_pct", pct),
         ("close_days", days), ("address", address)]
        if val is None]

    if missing:
        return {
            "error": f"Missing: {', '.join(missing)}. "
                     f"Try format: 725k 3% 21day Travis 1740 Grand Ave",
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
    if pct > 0.5:
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
            "error": f'Address "{address}" is too short. Include street number, name, and type (e.g., 1740 Grand Ave)',
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

    return result

if __name__ == "__main__":
    tests = [
        # Original format
        ("725k 3% 21day 1740 Grand Ave", 725000, 0.03, 21),
        ("725k 3% 21day Travis 1740 Grand Ave", 725000, 0.03, 21),
        ("650k 3% 30day Harris 123 Main St Houston", 650000, 0.03, 30),
        ("1.2m 10% 45days Bexar 500 Ocean Blvd San Antonio", 1200000, 0.10, 45),
        # Messy real-world inputs
        ("725000 3% 21day 1740 Grand Ave", 725000, 0.03, 21),
        ("Offer 725k, 3 percent down, close in 21 days, 1740 Grand Ave Austin", 725000, 0.03, 21),
        ("725k 3 down 21day 1740 Grand Ave", 725000, 0.03, 21),
        ("725,000 3% 21day 1740 Grand Ave", 725000, 0.03, 21),
        ("725k 3% 21 day close 1740 Grand Ave", 725000, 0.03, 21),
        ("1740 Grand Ave Austin, 725k, 3%, 21 days", 725000, 0.03, 21),
        ("725k 10% 30day 1740 Grand Ave", 725000, 0.10, 30),
        ("725000 3 percent 21 days 1740 Grand Ave, Austin TX 78701", 725000, 0.03, 21),
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

