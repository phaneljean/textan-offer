"""
co_filler.py -- draft a Colorado CBS1 (Contract to Buy and Sell Real Estate,
Residential, 2026) from a texted offer. The Colorado counterpart of
pdf_filler.fill_offer_pdf(), which routes here when parsed["state"] == "CO".

Field names are the same rect-verified CBS1 names co_audit.py checks
(see its docstring); every draft is run back through co_audit before the
agent sees it, via pdf_validator.validate_offer_pdf on the review page.

Deadlines. A text like "500k 20% 30day 123 Main St, Denver, CO" says
nothing about the ~35 dates in §3.1, and under §3.2 a blank deadline
DELETES that contingency -- leaving them blank would quietly strip the
buyer's loan, appraisal, inspection and title protections. So the draft
fills a standard buyer-side schedule (DEFAULT_OFFSETS: days after the
assumed MEC, i.e. the Acceptance Deadline), pulls anything that would land
on or after closing back to the day before, keeps every delivery ->
objection -> resolution chain in order, and moves weekend and §3.3.3
holiday dates to a business day. Provisions this text can't speak to (seller financing,
assumptions, survey, water/mineral rights, conditional sale) are written
as "N/A" so they read as deliberate, not forgotten. Lead-based paint rows
stay blank: they depend on the year built, and co_audit flags them.
None of this is a recommendation -- the SMS confirmation and review page
both tell the agent these are defaults to review.
"""
import os
import re
from datetime import date, datetime, timedelta

import fitz  # PyMuPDF -- writes checkbox appearance streams that render in Safari/Preview

from pdf_filler import OUTPUT_DIR, open_unique_output
from co_audit import is_business_day

TEMPLATE_PATH = os.environ.get("CBS1_TEMPLATE_PATH",
                               os.path.join(os.path.dirname(os.path.abspath(__file__)), "CBS1_2026.pdf"))
FILE_STEM = "CBS1"
DEFAULT_INSPECTION_DAYS = 10

# Days after MEC. "C-n" = n days before Closing Date.
DEFAULT_OFFSETS = {
    "Record Title Deadline and Tax Certificate": 7,
    "Record Title Objection Deadline": 14,
    "Off Record Title Deadline": 7,
    "Off Record Title Objection Deadline": 14,
    "Title Resolution Deadline": 17,
    "Sellers Property Disclosure Deadline": 3,
}
HOA_OFFSETS = {
    "Association Documents Deadline": 7,
    "Association Documents Termination Deadline": 14,
}
LOAN_OFFSETS = {
    "New Loan Application Deadline": 3,
    "New Loan Terms Deadline": "C-7",
    "New Loan Availability Deadline": "C-7",
    "Appraisal Deadline": "C-10",
    "Appraisal Objection Deadline": "C-9",
    "Appraisal Resolution Deadline": "C-7",
}
NOT_APPLICABLE = [
    "Third Party Right to PurchaseApprove Deadline",
    "Buyers Credit Information Deadline", "Disapproval of Buyers Credit Information Deadline",
    "Existing Loan Deadline", "Existing Loan Termination Deadline", "Loan Transfer Approval Deadline",
    "Seller or Private Financing Deadline",
    "New ILC or New Survey Deadline", "New ILC or New Survey Objection Deadline",
    "New ILC or New Survey Resolution Deadline",
    "Water Rights Examination Deadline", "Mineral Rights Examination Deadline",
    "Conditional Sale Deadline",
]
# Chains that must stay in order after clamping (mirrors co_audit.SEQUENCES).
CHAINS = [
    ["Record Title Deadline and Tax Certificate", "Record Title Objection Deadline", "Title Resolution Deadline"],
    ["Off Record Title Deadline", "Off Record Title Objection Deadline", "Title Resolution Deadline"],
    ["Association Documents Deadline", "Association Documents Termination Deadline"],
    ["New Loan Application Deadline", "New Loan Terms Deadline", "New Loan Availability Deadline"],
    ["Appraisal Deadline", "Appraisal Objection Deadline", "Appraisal Resolution Deadline"],
    ["Inspection Objection Deadline", "Inspection Resolution Deadline"],
    ["Due Diligence Documents Delivery Deadline", "Due Diligence Documents Objection Deadline",
     "Due Diligence Documents Resolution Deadline"],
]
LOAN_BOX = {"conventional": "Conventional Checkbox", "fha": "FHA Checkbox", "va": "VA Checkbox"}

# Cities that sit in a single county, for filling §2.4 when the text names
# only the city. Multi-county cities (Aurora, Arvada, Westminster, Littleton,
# Longmont...) are deliberately absent -- the agent texts the county.
CITY_COUNTY = {
    "denver": "Denver", "colorado springs": "El Paso", "fort collins": "Larimer", "loveland": "Larimer",
    "lakewood": "Jefferson", "golden": "Jefferson", "boulder": "Boulder", "pueblo": "Pueblo",
    "greeley": "Weld", "grand junction": "Mesa", "castle rock": "Douglas", "parker": "Douglas",
    "highlands ranch": "Douglas", "thornton": "Adams", "centennial": "Arapahoe", "broomfield": "Broomfield",
}

_SUFFIX = (r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|court|way|pl|place|cir|circle|"
           r"ter|terrace|pkwy|parkway|hwy|highway|trl|trail|loop|pass|run|cv|cove|xing|crossing|sq|square|walk)")
_STREET_RE = re.compile(r"^\s*(\d{1,6}\b.*?\b" + _SUFFIX + r"\b\.?)(.*)$", re.I)
_ZIP_RE = re.compile(r"\b(8[01]\d{3})(?:-\d{4})?\b")
CO_COUNTIES = [
    "Adams", "Alamosa", "Arapahoe", "Archuleta", "Baca", "Bent", "Boulder", "Broomfield", "Chaffee", "Cheyenne",
    "Clear Creek", "Conejos", "Costilla", "Crowley", "Custer", "Delta", "Denver", "Dolores", "Douglas", "Eagle",
    "El Paso", "Elbert", "Fremont", "Garfield", "Gilpin", "Grand", "Gunnison", "Hinsdale", "Huerfano", "Jackson",
    "Jefferson", "Kiowa", "Kit Carson", "La Plata", "Lake", "Larimer", "Las Animas", "Lincoln", "Logan", "Mesa",
    "Mineral", "Moffat", "Montezuma", "Montrose", "Morgan", "Otero", "Ouray", "Park", "Phillips", "Pitkin",
    "Prowers", "Pueblo", "Rio Blanco", "Rio Grande", "Routt", "Saguache", "San Juan", "San Miguel", "Sedgwick",
    "Summit", "Teller", "Washington", "Weld", "Yuma",
]  # all 64
_COUNTY_RE = re.compile(r"\b(" + "|".join(sorted(CO_COUNTIES, key=len, reverse=True)) + r")\s+county\b", re.I)
# Only as the state -- followed by a comma or the end -- so "Colorado Springs" keeps its name.
_STATE_RE = re.compile(r",?\s*\b(?:CO|Colo|Colorado)\b\.?(?=\s*(?:,|$))", re.I)


def split_co_address(text: str) -> dict:
    """'123 Main St, Denver, CO 80202' -> street/city/county/zip.
    Anything not stated stays blank rather than guessed (except the
    single-county city lookup above)."""
    out = {"street": "", "city": "", "county": "", "zip": ""}
    t = text or ""
    m = _ZIP_RE.search(t)
    if m:
        out["zip"] = m.group(1)
        t = t[:m.start()] + t[m.end():]
    m = _COUNTY_RE.search(t)
    if m:
        out["county"] = m.group(1).strip().title()
        t = t[:m.start()] + t[m.end():]
    t = _STATE_RE.sub(" ", t)
    m = _STREET_RE.match(t)
    if not m:
        out["street"] = re.sub(r"\s+", " ", t).strip(" ,.")
        return out
    out["street"] = m.group(1).strip(" ,")
    city = re.sub(r"\s+", " ", m.group(2)).strip(" ,.")
    if city.lower().endswith(" " + out["county"].lower()) and out["county"]:
        city = city[: -len(out["county"])].strip(" ,")
    out["city"] = city.title()
    if not out["county"]:
        out["county"] = CITY_COUNTY.get(city.lower(), "")
    return out


def _weekday(d: date, latest: date) -> date:
    """Weekend or §3.3.3 holiday -> the next business day, unless that passes
    `latest`; then the business day before."""
    fwd = d
    while not is_business_day(fwd):
        fwd += timedelta(days=1)
    if fwd <= latest:
        return fwd
    back = d
    while not is_business_day(back):
        back -= timedelta(days=1)
    return back


def compute_deadlines(mec: date, closing: date, financed: bool, has_hoa: bool = False,
                      inspection_days: int = None) -> dict:
    """{field name: 'MM/DD/YYYY' or 'N/A'} for every §3.1 row this draft fills."""
    insp = inspection_days if inspection_days else DEFAULT_INSPECTION_DAYS
    offsets = dict(DEFAULT_OFFSETS)
    offsets.update({
        "Inspection Termination Deadline": insp,
        "Inspection Objection Deadline": insp,
        "Inspection Resolution Deadline": insp + 3,
        "Property Insurance Termination Deadline": insp,
        "Due Diligence Documents Delivery Deadline": 5,
        "Due Diligence Documents Objection Deadline": insp,
        "Due Diligence Documents Resolution Deadline": insp + 3,
    })
    if has_hoa:
        offsets.update(HOA_OFFSETS)
    if financed:
        offsets.update(LOAN_OFFSETS)

    latest = closing - timedelta(days=1)
    dates = {}
    for name, off in offsets.items():
        d = closing - timedelta(days=int(off[2:])) if isinstance(off, str) else mec + timedelta(days=off)
        d = min(max(d, mec + timedelta(days=1)), latest)
        dates[name] = _weekday(d, latest)
    for chain in CHAINS:
        present = [n for n in chain if n in dates]
        for a, b in zip(present, present[1:]):
            if dates[b] < dates[a]:
                dates[b] = dates[a]

    values = {n: f"{d:%m/%d/%Y}" for n, d in dates.items()}
    na = list(NOT_APPLICABLE)
    if not has_hoa:
        na += list(HOA_OFFSETS)
    if not financed:
        na += list(LOAN_OFFSETS)
    values.update({n: "N/A" for n in na})
    return values


def contract_dates(close_days: int, today: date = None) -> tuple:
    """(acceptance/MEC date, closing date). Acceptance is the next weekday;
    closing is close_days out, moved off a weekend or holiday to the next business day."""
    today = today or datetime.now().date()
    acceptance = today + timedelta(days=1)
    while not is_business_day(acceptance):
        acceptance += timedelta(days=1)
    closing = today + timedelta(days=close_days)
    while not is_business_day(closing):
        closing += timedelta(days=1)
    return acceptance, closing


def _money(x) -> str:
    return f"{x:,.2f}"


def build_values(parsed: dict, today: date = None) -> dict:
    """Every CBS1 field value this draft sets, as {name: str | bool}."""
    price = int(parsed["price"])
    loan = int(parsed.get("loan_amount") or 0)
    earnest = int(parsed.get("earnest_money") or 0)
    cash = price - loan - earnest
    acceptance, closing = contract_dates(parsed["close_days"], today)
    addr = split_co_address(parsed.get("address", ""))
    county = parsed.get("county") or addr["county"]
    city = parsed.get("city") or addr["city"]

    values = {
        "Date": f"{(today or datetime.now().date()):%m/%d/%Y}",
        "Street Address": addr["street"],
        "City": city,
        "State": "CO",
        "Zip Code": parsed.get("zip") or addr["zip"],
        "County": county,
        "Purchase Price": _money(price),
        "Earnest Money": _money(earnest),
        "Cash At Closing": _money(cash),
        "Total 01": _money(price),
        "Total 02": _money(price),
        "Closing Date": f"{closing:%m/%d/%Y}",  # never a weekend/holiday (contract_dates)
        "Possession Date": f"{closing:%m/%d/%Y}",
        "Acceptance Deadline Date": f"{acceptance:%m/%d/%Y}",
        "Acceptance Deadline Time": "5:00 PM",
    }
    if loan > 0:
        values["New Loan"] = _money(loan)
        values[LOAN_BOX.get(parsed.get("financing_type") or "conventional", "Conventional Checkbox")] = True
    agent = parsed.get("agent") or {}
    if agent.get("name"):
        values["Broker 1 Name"] = agent["name"]
    values.update(compute_deadlines(acceptance, closing, financed=loan > 0, has_hoa=bool(parsed.get("has_hoa")),
                                    inspection_days=parsed.get("inspection_days")))
    return {k: v for k, v in values.items() if v not in ("", None)}


def fill_cbs1(parsed: dict, agent_phone: str = None, today: date = None) -> str:
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(f"CBS1 template not found at {TEMPLATE_PATH}. Set CBS1_TEMPLATE_PATH.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    values = build_values(parsed, today)
    doc = fitz.open(TEMPLATE_PATH)
    for page in doc:
        for w in page.widgets():
            if w.field_name not in values:
                continue
            v = values[w.field_name]
            if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                w.field_value = w.on_state() if v else "Off"
            else:
                w.field_value = str(v)
            w.update()
    doc.set_metadata({**doc.metadata, "producer": "TxtAnOffer", "subject": "Colorado CBS1 draft"})
    safe_addr = "".join(ch for ch in (parsed.get("address") or "offer").split(",")[0] if ch.isalnum())[:30]
    f, out_path = open_unique_output(OUTPUT_DIR, f"{FILE_STEM}_{safe_addr}")
    with f:
        f.write(doc.tobytes(garbage=3, deflate=True))
    doc.close()
    return out_path
