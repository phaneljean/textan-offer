"""
parser.py — turns a freeform SMS like:
 "725k 3% 21day 1740 Grand Ave"
into structured offer data.
No LLM call needed for the common patterns. Falls back to
returning an 'error' key with a hint message if it can't parse.
"""
import re

PRICE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([kK]|million|mil|m\b)?')
PCT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*%')
DAYS_RE = re.compile(r'(\d+)\s*day')
# Common TX counties - agents can specify to avoid geocoding lookup
TX_COUNTIES = [
    'travis', 'harris', 'bexar', 'dallas', 'tarrant', 'collin', 'denton',
    'williamson', 'fort bend', 'montgomery', 'el paso', 'hidalgo', 'cameron',
]
COUNTY_RE = re.compile(r'\b(' + '|'.join(TX_COUNTIES) + r')\b', re.IGNORECASE)

def _parse_price(text):
    # look for a number followed by k/m, prioritizing this over bare percentages/days
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(k|m|million|mil)\b', text, re.IGNORECASE):
        num = float(m.group(1))
        unit = m.group(2).lower()
        if unit == 'k':
            return int(num * 1_000)
        else:
            return int(num * 1_000_000)
    return None

def _parse_pct(text):
    m = PCT_RE.search(text)
    return float(m.group(1)) / 100 if m else None

def _parse_days(text):
    m = DAYS_RE.search(text)
    return int(m.group(1)) if m else None

def _parse_county(text):
    # Look for county name in the text
    m = COUNTY_RE.search(text)
    return m.group(1).capitalize() if m else None

def _parse_address(text):
    # crude heuristic: strip out the price/pct/day/county tokens, what's left is the address
    stripped = re.sub(r'\d+(?:\.\d+)?\s*(k|m|million|mil)\b', '', text, flags=re.IGNORECASE)
    stripped = re.sub(r'\d+(?:\.\d+)?\s*%', '', stripped)
    stripped = re.sub(r'\d+\s*day\w*', '', stripped, flags=re.IGNORECASE)
    # Remove county name if present
    stripped = COUNTY_RE.sub('', stripped)
    address = stripped.strip(' ,.-')
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

    # Add county if specified (optional)
    if county:
        result["county"] = county

    return result

if __name__ == "__main__":
    # quick manual tests
    tests = [
        "725k 3% 21day 1740 Grand Ave",
        "725k 3% 21day Travis 1740 Grand Ave",
        "650k 3% 30day Harris 123 Main St Houston",
        "1.2m 10% 45days Bexar 500 Ocean Blvd San Antonio",
    ]
    for t in tests:
        result = parse_offer_sms(t)
        county = result.get('county', 'unspecified') if 'error' not in result else 'N/A'
        print(f"{t} -> county={county}")

