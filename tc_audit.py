"""
tc_audit.py — Standalone field-completeness audit for a TREC 20-19 AcroForm
PDF uploaded by a transaction coordinator, independent of this app's own
offer-generation flow (see app.py's process_offer() / pdf_filler.py).

This is NOT the same check as pdf_validator.validate_offer_pdf(): that
function checks a PDF THIS APP just generated against the `parsed` dict THIS
APP's own parser produced -- it has an external source of truth. A TC's
uploaded file could have been filled by any tool, so there is no ground
truth to check it against here. v1 is therefore internal-consistency-only:
did the fields this app has already rect-verified (see pdf_filler.py's
FIELD_MAP comments) actually get filled in.

Only fields with a confirmed on-page position are checked. Since 2026-10-09
that includes per-page initials (found by position, one per party),
Paragraph 3 price math, the 9A closing date, option-period days,
"check one box only" conflicts, 12B broker contributions, page-12
receipts, page header addresses and Paragraph 21 notice emails -- all
rect-verified by rendering a filled test file (see _consistency_issues).
Earlier comments in this file saying closing date and option period have
NO AcroForm field were wrong: both fields exist. This app's own generator
leaves them empty and draws the value as an overlay, so those checks read
"field value, else text drawn inside the field's box".

v1 also assumes the uploaded PDF is AcroForm-fillable (not a flattened scan)
and was filled using field names matching this app's own 20-19_2.pdf
template. A PDF from a different tool/source/form revision may use entirely
different internal field names -- if too few of the checked fields are even
present in the uploaded file, this reports the file as unrecognized rather
than claiming everything on it is "missing".
"""
import re
from datetime import date
from pypdf import PdfReader
from pdf_validator import _read_values, _is_checked, _money_to_int
from pdf_filler import FIELD_MAP
from financing_addendum import FIELD_MAP as FA_FIELDS
from amendment import FIELD_MAP as AMEND_FIELDS

# (FIELD_MAP key, message shown to the TC, blocking)
# "blocking" mirrors the same product decision pdf_validator.py already
# made for these exact fields: buyer/seller legal names are never collected
# by this app's own generation flow either, so their absence is a warning,
# not a blocker, here too.
CHECKED_FIELDS = [
    ("address", "Section 2A: Property address is blank", True),
    ("city", "Section 2A: City is blank", True),
    ("county", "Section 2A: County is blank -- title will kick back the file without this", True),
    ("buyer_name", "Section 1: Buyer legal name is blank", False),
    ("seller_name", "Section 1: Seller legal name is blank", False),
    ("escrow_agent_name", "Section 5A: Escrow Agent name is blank", True),
    ("earnest_money_amount", "Section 5A: Earnest money amount is blank", True),
    ("option_fee_amount", "Section 5A: Option fee amount is blank", True),
    ("title_company", "Section 6A: Title Company is blank", True),
]

# Below this many matched field names, treat the upload as a template we
# don't recognize rather than reporting every checked field as "missing" --
# a PDF from a different tool/source won't use this app's field names at all.
MIN_MATCHED_FIELDS = 3

# Same idea, applied to a standalone 40-11 upload's OWN raw field names
# (financing_addendum.py's FIELD_MAP) rather than the merged-file FA_-
# prefixed convention -- see check_tc_file()'s two-file path below.
MIN_MATCHED_FIELDS_FA = 3

# Same idea for a standalone TREC 39-11 Amendment to Contract upload
# (amendment.py's FIELD_MAP -- only 6 keys total, since that form is
# mostly free-text paragraphs this app's own generator never fills; see
# amendment.py's own docstring). Unlike the 40-11 case, there is no
# merged-into-main-file convention for an amendment -- it's always its
# own separate PDF -- so this is only ever checked in the multi-file path.
MIN_MATCHED_AMEND = 2

# If most of the core required fields (address, county, title company,
# escrow agent, earnest money, option fee -- NOT initials/Effective
# Date/addendum, which a nearly-finished file can legitimately still be
# missing right before closing) are blank, this isn't a file with a few
# fixable gaps -- it's an essentially blank draft. Cheaper for the TC to
# regenerate cleanly than to chase down that many individual corrections.
CORE_BLOCKING_FIELDS = sum(1 for _, _, blocking in CHECKED_FIELDS if blocking)
BLANK_DRAFT_THRESHOLD = 0.7  # fraction of CORE_BLOCKING_FIELDS missing

# Effective Date -- TREC 20-19 page 10 of 12: "EXECUTED the ___ day of ___,
# 20__ (Effective Date). (BROKER: FILL IN THE DATE OF FINAL ACCEPTANCE.)"
# Rect-verified 2026-08-30 by rendering distinct markers into each field and
# confirming visually against the printed blank -- none of these 3 raw names
# describe their own role (another instance of TREC's export-tool naming
# lying about position). NOT in pdf_filler.py's FIELD_MAP -- this app never
# fills Effective Date (it's the broker's to fill in on final acceptance,
# same reasoning as buyer/seller signatures), so it was never rect-verified
# until now. Note: FIELD_MAP's "closing_year_suffix": "20_2" entry is a
# stale/unused mismap -- "20_2" is actually this Effective Date year field,
# not a closing-date field (fill_offer_pdf() never references that key at
# all; closing date is drawn entirely via reportlab overlay).
EFFECTIVE_DATE_FIELDS = {
    "day": "EXECUTED the",
    "month": "day of",
    "year": "20_2",
}

# Initials for identification -- found by POSITION, not field name
# (2026-10-09). Every 20-19 page 1-9 has four boxes on the footer line
# "Initialed for identification by Buyer ___ ___ and Seller ___ ___",
# rect-verified on all nine pages by rendering: x0 = 211/252/347/396,
# bottom ~20-21pt. Ordered left to right they are Buyer1, Buyer2, Seller1,
# Seller2. Their /T names are unusable (pages 2, 3 and 7 are named
# "2 MEMBERSHIP IN PROPERTY...", "Property Code requires...", "AC numb 1-4";
# on page 8 "and Seller_18" is the BUYER2 box), which is why an earlier
# name-based table silently skipped pages 2, 3 and 7.
INITIALS_X_RANGE = (200, 440)
INITIALS_MAX_BOTTOM = 40

# Same quad on the 40-11 Third Party Financing Addendum's own page 1 of 2 --
# rect-verified 2026-08-30 the same way. Only checked when the addendum is
# actually attached (its fields are namespaced "FA_" + raw name by
# financing_addendum.py at merge time -- see pdf_validator.py's same FA_
# convention). No buyer/seller NAME field exists anywhere on this addendum
# template (checked directly -- only loan-amount and initials fields do), so
# a name cross-check between the main contract and this addendum is not
# buildable; only the addendum's own initials-completeness is checked here.
FA_PREFIX = "FA_"
FA_INITIALS_PAGE = ("40-11 addendum", "Initialed for identification by Buyer", "undefined_2", "and Seller", "undefined_3")

# Addendum-vs-contract internal consistency. Both sides of each comparison
# live in the SAME uploaded document, so this needs no external "parsed"
# ground truth (unlike pdf_validator.py's equivalent check, which compares
# against parsed["loan_amount"] because it's validating a freshly-generated
# draft rather than an arbitrary upload). Loan amount uses FA_FIELDS'
# "first_loan_amount" (already used -- and thus already trusted -- by
# financing_addendum.py's own fill logic) against pdf_filler.py's
# already-verified "loan_amount" (Section 3B). Checkbox names likewise
# reuse pdf_filler.py's already rect-verified "third_party_financing_3b"
# (Sec 3B row) and "third_party_financing" (Sec 22 addenda list).


# Contract-vs-Amendment cross-checks. Both sides live in separate uploaded
# PDFs -- the main contract's own values dict, and the Amendment's --
# compared the same way the 40-11 loan-amount check above compares two
# separate dicts. Scope is deliberately narrow: amendment.py's FIELD_MAP
# only has rect-verified fields for price and address. A closing-date
# cross-check is NOT included yet. (Correction 2026-10-09: the 20-19 DOES
# have a 9A field, "A The closing of the sale will be on or before"; this
# app's own generator overlays the date instead of filling it. See
# SLOTS["closing_date"] for how the single-file check reads both.)
# Buyer/seller names, financing type, earnest money, option fee, and the
# amendment's free-text "other modifications" paragraph are out of scope
# for the same reason CHECKED_FIELDS' docstring gives for v1 in general:
# no rect-verified field mapping exists for them, and a wrong "match"/
# "mismatch" signal on an unverified field is worse than no signal.
AMEND_PRICE_MISMATCH_KEY = "amendment_price_mismatch"
AMEND_ADDRESS_MISMATCH_KEY = "amendment_address_mismatch"


def _matched_amend(values: dict) -> int:
    return sum(1 for name in AMEND_FIELDS.values() if name in values)


def _normalized_address(raw: str) -> str:
    # Mirrors pdf_filler.py's own stripping of a trailing "TX"/"Texas" before
    # filling the main contract's address field (that form already prints
    # ", Texas" as static text right after the blank -- see its FIELD_MAP
    # comment). A third-party-filled amendment has no reason to follow that
    # same convention, so without stripping both sides the same way, this
    # app's own contract+amendment pair would false-positive on every single
    # file purely from that formatting difference, not an actual mismatch.
    cleaned = re.sub(r",?\s*\b(TX|Texas)\b\.?\s*$", "", raw, flags=re.IGNORECASE).strip(" ,")
    return " ".join(cleaned.upper().split())


_ENTITY_WORDS = re.compile(r"\b(LLC|L\.L\.C|INC|CORP|CORPORATION|COMPANY|CO|LP|LLP|LTD|TRUST|TRUSTEE|ESTATE|BANK|PARTNERS|HOLDINGS|PROPERTIES|HOMES|GROUP)\b", re.I)
_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def _party_names(raw: str) -> list:
    """'John Doe and Mary Doe' -> ['John Doe', 'Mary Doe']. Blank -> []."""
    parts = re.split(r"\s+and\s+|\s*&\s*|\s*;\s*|\s*/\s*", (raw or "").strip(), flags=re.I)
    return [p.strip(" ,") for p in parts if p.strip(" ,")]


def _initials_match(initials: str, name: str):
    """True/False for a person's name; None when it can't be judged (an
    entity like 'ABC Homes LLC' is initialed by whoever signs for it)."""
    if _ENTITY_WORDS.search(name):
        return None
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'.-]*", name) if w.upper().strip(".") not in _NAME_SUFFIXES]
    letters = re.sub(r"[^A-Za-z]", "", initials).upper()
    if len(words) < 2 or len(letters) < 2:
        return None
    return letters[0] == words[0][0].upper() and letters[-1] == words[-1][0].upper()


def _check_initials_pair(page_label: str, buyer_boxes: list, seller_boxes: list,
                         buyers: list, sellers: list) -> list:
    """One initial per party per page. The party count comes from the
    Paragraph 1 names (one buyer named = only the first buyer box is
    required; a blank second box is correct, not missing)."""
    issues = []
    for role, boxes, names, key in (("Buyer", buyer_boxes, buyers, "initials_buyer"),
                                    ("Seller", seller_boxes, sellers, "initials_seller")):
        filled = [b.strip() for b in boxes if b and b.strip()]
        needed = min(max(len(names), 1), len(boxes))
        if len(filled) < needed:
            who = f"{role.lower()} initials missing" if needed == 1 else f"{role.lower()} initials missing ({len(filled)} of {needed})"
            issues.append({"severity": "blocker", "message": f"{page_label}: {who[0].upper() + who[1:]}", "key": key})
        for ini in filled:
            verdicts = [_initials_match(ini, n) for n in names]
            if verdicts and None not in verdicts and not any(verdicts):
                issues.append({
                    "severity": "warning",
                    "message": f'{page_label}: {role} initials "{ini}" don\'t match the {role.lower()} named in Paragraph 1 ({" and ".join(names)})',
                    "key": "initials_mismatch",
                })
    return issues


def _widget_rows(page) -> list:
    """(name, value, field_type, rect) for every form widget on a page."""
    rows = []
    for a in page.get("/Annots") or []:
        a = a.get_object()
        if a.get("/Subtype") != "/Widget":
            continue
        parent = a.get("/Parent")
        parent = parent.get_object() if parent is not None else None
        name = a.get("/T") if a.get("/T") is not None else (parent.get("/T") if parent is not None else None)
        value = a.get("/V") if a.get("/V") is not None else (parent.get("/V") if parent is not None else None)
        ft = a.get("/FT") or (parent.get("/FT") if parent is not None else None)
        rows.append((str(name or ""), "" if value is None else str(value), str(ft or ""), [float(x) for x in a["/Rect"]]))
    return rows


def _contract_pages(reader) -> dict:
    """{printed page number: page} for the TREC 20-19 pages in a file,
    using each page's own printed footer/header text, so a cover page or a
    merged addendum never shifts the numbering."""
    out = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        if not re.search(r"TREC NO\.?\s*20-\d+", text):
            continue
        m = re.search(r"Page\s*(\d+)\s*of\s*12", text)
        n = int(m.group(1)) if m else (1 if not out else None)
        if n is not None and n not in out:
            out[n] = page
    return out


def _initials_by_position(pages: dict) -> list:
    """[(page label, [buyer1, buyer2], [seller1, seller2])] for each 20-19
    page whose footer has exactly the four initials boxes."""
    found = []
    for n in sorted(pages):
        boxes = []
        for name, value, ft, (x0, y0, x1, y1) in _widget_rows(pages[n]):
            if (ft == "/Tx" and not name.startswith(FA_PREFIX)
                    and INITIALS_X_RANGE[0] <= x0 <= INITIALS_X_RANGE[1] and min(y0, y1) < INITIALS_MAX_BOTTOM):
                boxes.append((x0, value))
        if len(boxes) == 4:
            boxes.sort()
            found.append((f"Page {n} of 12", [boxes[0][1], boxes[1][1]], [boxes[2][1], boxes[3][1]]))
    return found


def _text_in_rect(page, rect) -> str:
    """Text DRAWN on the page inside a box (not a form-field value). This
    app's own generator writes closing date, option period and the page-11
    header as reportlab overlays on top of an empty field, so a check must
    read 'field value, else overlay text in the same box'."""
    x0, y0, x1, y1 = rect
    max_chars = max(4, int((x1 - x0) / 3.5))   # what can physically fit in the box
    parts = []

    def visit(text, cm, tm, font_dict, font_size):
        # The form's own printed line ("_____ days after the Effective
        # Date...") can start inside the same box; it's far longer than the
        # box, so length separates it from a value drawn into the blank.
        text = text.replace("_", " ").strip()
        if not text or len(text) > max_chars:
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        if x0 - 8 <= x <= x1 and y0 - 2 <= y <= y1:
            parts.append(text)
    page.extract_text(visitor_text=visit)
    return " ".join("".join(parts).split())


# --- Internal-consistency rules (2026-10-09) --------------------------------
# Every field below was rect-verified 2026-10-09 by rendering a filled test
# 20-19 with each widget's box and index drawn on the page. Several /T names
# describe a DIFFERENT blank (e.g. the 5B option-period days box is named
# "the Title Company and Buyers lenders Check one box only"); trust the
# comment, not the name.
SURVEY_BOXES = [("6C(1)", "Buyer"), ("6C(2)", "Within three"), ("6C(3)", "Within four")]
DISCLOSURE_BOXES = [("7B(1)", "1 Buyer accepts the Property As Is"),
                    ("7B(2)", "2 Buyer accepts the Property As Is provided Seller at Sellers expense shall complete the"),
                    ("7B(3)", "upon")]
AS_IS_BOXES = [("7D(1)", "As Is"), ("7D(2)", "As Is except")]
POA_BOXES = [("is", "1Within"), ("is not", "2 Within")]   # 6E(2) "The Property [ ] is [ ] is not subject to..."
POSSESSION_BOXES = [("upon closing and funding", "will"),
                    ("temporary residential lease", "will not be credited to the Sales Price at closing Time is of the")]
# 12B: (label, row checkbox, $ checkbox, $ amount, % checkbox, % amount)
BROKER_CONTRIB = [
    ("12B(1) (Seller pays toward Buyer's broker)", "Seller as List Brok Sub agent", "Seller as List Brok Sub agent27",
     "acknowledged by Seller and Buyers agreement to pay Seller 130", "Seller only as Sellers agent",
     "acknowledged by Seller and Buyers agreement to pay Seller 31"),
    ("12B(2) (Buyer pays toward Seller's broker)", "Dollar Amt4", "Dollar Amt5",
     "acknowledged by Seller and Buyers agreement to pay Seller 32", "Percentage",
     "acknowledged by Seller and Buyers agreement to pay Seller 40"),
]
# Value slots: (printed page, field name, rect in PDF points). Read as the
# field's value, else overlay text drawn inside the rect (this app's own
# generator overlays closing date, option days and the page-11 header).
SLOTS = {
    "option_days": (2, "the Title Company and Buyers lenders Check one box only", (76, 496, 109, 505)),
    "disclosure_days": (4, "Within", (424, 203, 478, 213)),
    "closing_date": (6, "A The closing of the sale will be on or before", (291, 668, 422, 678)),
    "closing_year": (6, "20", (442, 668, 469, 678)),
}
HEADER_FIELDS = [(2, "Page 2 of 10"), (3, "Page 3 of 10"), (4, "Contract Concerning"), (5, "Contract Concerning_2"),
                 (6, "Contract Concerning_3"), (7, "Page 7 of 10"), (8, "Contract Concerning_4"),
                 (9, "Address of Property"), (10, "Addr of Prop"), (11, "Address of Property_2"),
                 (12, "Address of Property_26")]
HEADER_RECT = (120, 745, 440, 766)   # stops before the printed "Page N of 12"
RECEIPTS = [("Option Fee receipt (page 12)", "is acknowledged", "option_fee_amount", "option fee in Paragraph 5A"),
            ("Earnest Money receipt (page 12)", "is acknowledged_2", "earnest_money_amount", "earnest money in Paragraph 5A")]
NOTICE_EMAILS = [("Buyer's notice email (Paragraph 21)", "undefined_2013"),
                 ("Seller's notice email (Paragraph 21)", "undefined numb 2214"),
                 ("Buyer's agent email (Paragraph 21 copy)", "undefined_20"),
                 ("Listing agent email (Paragraph 21 copy)", "undefined numb 22")]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_STREET_ABBR = {"STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "BOULEVARD": "BLVD",
                "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL", "PARKWAY": "PKWY", "TRAIL": "TRL", "HIGHWAY": "HWY"}


def _money(text: str):
    """'$425,000.00' -> 425000.0; blank/unreadable -> None. (Unlike
    _money_to_int, '425000' and '425,000.00' compare equal.)"""
    cleaned = re.sub(r"[^\d.]", "", text or "")
    try:
        return float(cleaned) if cleaned.strip(".") else None
    except ValueError:
        return None


def _fmt_money(x: float) -> str:
    return f"${x:,.0f}" if x == int(x) else f"${x:,.2f}"


def _street(addr: str) -> str:
    first = (addr or "").split(",")[0].upper()
    words = re.sub(r"[^A-Z0-9 ]", " ", first).split()
    return " ".join(_STREET_ABBR.get(w, w) for w in words)


def _checked_labels(values: dict, boxes: list) -> list:
    return [label for label, name in boxes if _is_checked(values, name)]


def _slot(values: dict, pages: dict, key: str) -> str:
    page_no, name, rect = SLOTS[key]
    v = values.get(name, "").strip()
    if v or page_no not in pages:
        return v
    return _text_in_rect(pages[page_no], rect)


def _closing_date_problem(text: str, year_suffix: str):
    """Message if the 9A date can't exist (e.g. 'November 31'); None if
    fine or unparseable (an unreadable date is not proof of an error)."""
    t = text.strip().lower().replace(",", " ")
    m = re.match(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m and m.group(1)[:3] in MONTHS:
        month, day = MONTHS[m.group(1)[:3]], int(m.group(2))
    else:
        m = re.match(r"(\d{1,2})\s*/\s*(\d{1,2})\b", t)
        if not m:
            return None
        month, day = int(m.group(1)), int(m.group(2))
    ys = re.sub(r"\D", "", year_suffix or "")
    year = 2000 + int(ys[-2:]) if ys else 2028   # unknown year: use a leap year so Feb 29 isn't flagged
    try:
        date(year, month, day)
        return None
    except ValueError:
        return f'Paragraph 9A: Closing date "{text.strip()}" is not a real date'


def _consistency_issues(values: dict, pages: dict) -> list:
    issues = []

    def add(sev, msg, key):
        issues.append({"severity": sev, "message": msg, "key": key})

    # 3A + 3B = 3C
    cash, loan, price = (_money(values.get(FIELD_MAP[k], "")) for k in ("down_payment", "loan_amount", "sales_price"))
    if price is not None and cash is not None:
        total = cash + (loan or 0)
        if abs(total - price) > 0.5:
            add("blocker", f"Paragraph 3: Cash portion (3A) {_fmt_money(cash)} + financing (3B) {_fmt_money(loan or 0)} = "
                           f"{_fmt_money(total)}, but the Sales Price (3C) says {_fmt_money(price)}", "sales_price_math")

    # 9A closing date is a real date
    closing = _slot(values, pages, "closing_date")
    if closing:
        problem = _closing_date_problem(closing, _slot(values, pages, "closing_year"))
        if problem:
            add("blocker", problem, "closing_date_invalid")

    # 5B option period days when an option fee is entered
    if values.get(FIELD_MAP["option_fee_amount"], "").strip() and not _slot(values, pages, "option_days"):
        add("blocker", "Paragraph 5B: Option fee is entered but the option period (number of days) is blank", "option_days_blank")

    # "Check one box only" groups
    for label, boxes in (("Paragraph 6C (Survey)", SURVEY_BOXES), ("Paragraph 7B (Seller's Disclosure)", DISCLOSURE_BOXES),
                         ("Paragraph 7D (Acceptance of condition)", AS_IS_BOXES), ("Paragraph 6E(2) (Owners association)", POA_BOXES),
                         ("Paragraph 10A (Possession)", POSSESSION_BOXES)):
        checked = _checked_labels(values, boxes)
        if len(checked) > 1:
            add("blocker", f"{label}: {' and '.join(checked)} are both checked -- only one box is allowed", "check_one_conflict")

    # 7B(2) needs its delivery days
    if _is_checked(values, DISCLOSURE_BOXES[1][1]) and not _slot(values, pages, "disclosure_days"):
        add("blocker", "Paragraph 7B(2): Seller's Disclosure delivery days are blank", "disclosure_days_blank")

    # 12B broker contributions
    for label, row, d_box, d_amt, p_box, p_amt in BROKER_CONTRIB:
        if not _is_checked(values, row):
            continue
        d_on, p_on = _is_checked(values, d_box), _is_checked(values, p_box)
        if not d_on and not p_on:
            add("blocker", f"Paragraph {label}: checked, but neither the $ box nor the % box is", "broker_contribution_incomplete")
        elif d_on and p_on:
            add("blocker", f"Paragraph {label}: both the $ box and the % box are checked -- only one is allowed", "broker_contribution_incomplete")
        elif d_on and not values.get(d_amt, "").strip():
            add("blocker", f"Paragraph {label}: $ box checked but the amount is blank", "broker_contribution_incomplete")
        elif p_on and not values.get(p_amt, "").strip():
            add("blocker", f"Paragraph {label}: % box checked but the percentage is blank", "broker_contribution_incomplete")

    # Owners association disclosed but its addendum not listed in Paragraph 22
    if _is_checked(values, POA_BOXES[0][1]) and not _is_checked(values, FIELD_MAP["hoa_addendum"]):
        add("warning", "Paragraph 6E(2) says the property IS subject to an owners association, but the Owners Association "
                       "Addendum isn't checked in Paragraph 22 -- attach it or correct 6E(2)", "poa_addendum_missing")

    # Receipts on page 12 vs. Paragraph 5A
    for label, receipt_field, key, what in RECEIPTS:
        got, want = _money(values.get(receipt_field, "")), _money(values.get(FIELD_MAP[key], ""))
        if got is not None and want is not None and abs(got - want) > 0.005:
            add("blocker", f"{label} says {_fmt_money(got)}, but the {what} is {_fmt_money(want)}", "receipt_mismatch")

    # Address header on every page vs. Paragraph 2A
    street = _street(values.get(FIELD_MAP["address"], ""))
    if street:
        for page_no, name in HEADER_FIELDS:
            if page_no not in pages:
                continue
            header = values.get(name, "").strip() or _text_in_rect(pages[page_no], HEADER_RECT)
            if not header:
                add("warning", f"Page {page_no} of 12: \"Address of Property\" header is blank", "header_address")
            elif _street(header) != street:
                add("blocker", f'Page {page_no} of 12: header address reads "{header}" but Paragraph 2A says '
                               f'"{values.get(FIELD_MAP["address"], "").strip()}"', "header_address")

    # Notice emails
    for label, name in NOTICE_EMAILS:
        v = values.get(name, "").strip()
        if v and not EMAIL_RE.match(v):
            add("warning", f'{label} "{v}" is not a valid email address -- notices sent there will bounce', "email_invalid")

    return issues


def _matched_main(values: dict) -> int:
    return sum(1 for key, _, _ in CHECKED_FIELDS if FIELD_MAP[key] in values)


def _matched_fa(values: dict) -> int:
    return sum(1 for name in FA_FIELDS.values() if name in values)


# Traffic-light rollup of an issues list -- pure arithmetic over data
# check_tc_file() already computes (severity + count), not a new signal.
# "attention" fires on ANY blocker, including the single-issue
# "unrecognized file" case, which is already a blocker -- no special case
# needed for that path. Kept as three flat levels, not a numeric score:
# a single number inviting a false sense of precision (is 6 issues twice
# as bad as 3?) is exactly the kind of overclaim this module's docstring
# already warns against for individual fields.
def _severity_summary(issues: list) -> dict:
    blockers = sum(1 for i in issues if i.get("severity") == "blocker")
    if blockers:
        return {"level": "attention", "emoji": "\U0001F534", "label": "Needs attention", "issue_count": len(issues)}
    if issues:
        return {"level": "review", "emoji": "\U0001F7E1", "label": "Review recommended", "issue_count": len(issues)}
    return {"level": "clear", "emoji": "\U0001F7E2", "label": "No issues detected", "issue_count": 0}


def check_tc_file(pdf_paths) -> dict:
    """Audits an uploaded TREC 20-19 AcroForm PDF for missing required
    fields, optionally cross-checked against its 40-11 Third Party
    Financing Addendum and/or its 39-11 Amendment to Contract, each
    uploaded as its own SEPARATE PDF.

    pdf_paths is a list of 1-3 file paths, in any order -- each is read
    independently and classified by field-name fingerprint (whichever
    matches CHECKED_FIELDS's template is the contract; whichever matches
    FA_FIELDS's own raw names is the addendum; whichever matches
    AMEND_FIELDS's raw names is the amendment). This is the realistic case:
    a TC's 40-11 or 39-11 is its own separate PDF filled by whatever tool
    they used, so it never carries this app's internal FA_-prefix -- that
    convention only exists on a PDF THIS app generated and merged itself
    (see financing_addendum.py). A single merged upload with that prefix
    still works too, unchanged from before, when only one path is given.
    There is no equivalent merged-amendment convention -- an amendment is
    always its own file.

    Returns {"recognized": bool, "complete": bool, "issues": [...]}.
    Raises whatever pypdf raises on a file that isn't a readable PDF at all --
    callers should catch that and turn it into a 400, not a 500."""
    # Page count only, for the upload-result metadata bar -- a second,
    # cheap PdfReader open (separate from _read_values' own) rather than
    # threading a reader object through pdf_validator.py's private helper.
    page_count = sum(len(PdfReader(p).pages) for p in pdf_paths)
    all_values = [_read_values(p) for p in pdf_paths]

    main_values = None
    main_path = None
    fa_values = None  # always raw (un-prefixed) FA_FIELDS keys, whichever source it came from
    amend_values = None
    if len(all_values) == 1:
        values = all_values[0]
        if _matched_main(values) >= MIN_MATCHED_FIELDS:
            main_values = values
            main_path = pdf_paths[0]
            # Addendum already merged into this same PDF via pdf_filler.py's FA_ prefix.
            if any(k.startswith(FA_PREFIX) for k in values):
                fa_values = {k[len(FA_PREFIX):]: v for k, v in values.items() if k.startswith(FA_PREFIX)}
    else:
        for path, values in zip(pdf_paths, all_values):
            m, f, am = _matched_main(values), _matched_fa(values), _matched_amend(values)
            if m >= MIN_MATCHED_FIELDS and m >= f and m >= am:
                main_values = values
                main_path = path
            elif f >= MIN_MATCHED_FIELDS_FA and f >= am:
                fa_values = values
            elif am >= MIN_MATCHED_AMEND:
                amend_values = values

    if main_values is None:
        unrecognized_issues = [{
            "severity": "blocker",
            "message": (
                "This doesn't look like a TREC 20-19 form we recognize -- "
                "field names didn't match our template. Only AcroForm-fillable "
                "20-19 PDFs are supported in this version (not scanned or flattened files)."
            ),
            "key": "unrecognized",
        }]
        return {
            "recognized": False,
            "complete": False,
            "issues": unrecognized_issues,
            "looks_like_blank_draft": False,
            "page_count": page_count,
            "has_addendum": False,
            "has_amendment": False,
            "severity": _severity_summary(unrecognized_issues),
        }

    values = main_values
    issues = []
    core_missing = 0
    for key, message, blocking in CHECKED_FIELDS:
        val = values.get(FIELD_MAP[key], "").strip()
        if not val:
            issues.append({"severity": "blocker" if blocking else "warning", "message": message, "key": key})
            if blocking:
                core_missing += 1

    # Effective Date
    missing_parts = [label for label, raw in EFFECTIVE_DATE_FIELDS.items() if not values.get(raw, "").strip()]
    if missing_parts:
        issues.append({"severity": "blocker", "message": "Page 10 of 12: Effective Date is blank", "key": "effective_date"})

    # Initials for identification, main contract -- one per party, boxes
    # found by position on each printed page.
    pages = _contract_pages(PdfReader(main_path))
    buyers = _party_names(values.get(FIELD_MAP["buyer_name"], ""))
    sellers = _party_names(values.get(FIELD_MAP["seller_name"], ""))
    for page_label, buyer_boxes, seller_boxes in _initials_by_position(pages):
        issues.extend(_check_initials_pair(page_label, buyer_boxes, seller_boxes, buyers, sellers))

    issues.extend(_consistency_issues(values, pages))

    has_addendum = fa_values is not None
    has_amendment = amend_values is not None

    # One or more extra files were uploaded but didn't match either the
    # 40-11 or 39-11 template -- surface that plainly instead of silently
    # skipping the cross-checks. (main_values itself always accounts for
    # exactly one of pdf_paths, so any gap between "files supplied" and
    # "files classified" means something didn't match.)
    extra_files = len(pdf_paths) - 1
    matched_extra = (1 if has_addendum else 0) + (1 if has_amendment else 0)
    if extra_files > matched_extra:
        issues.append({
            "severity": "warning",
            "message": "One of the uploaded files wasn't recognized as a TREC 40-11 Third Party Financing Addendum or 39-11 Amendment to Contract -- its consistency checks were skipped.",
            "key": "extra_file_unrecognized",
        })

    # Initials on the 40-11 addendum -- only if actually attached.
    if has_addendum:
        label, b1, b2, s1, s2 = FA_INITIALS_PAGE
        issues.extend(_check_initials_pair(label, [fa_values.get(b1, ""), fa_values.get(b2, "")],
                                           [fa_values.get(s1, ""), fa_values.get(s2, "")], buyers, sellers))

    # 1. Loan amount: main contract Section 3B vs. 40-11 principal amount.
    if has_addendum:
        main_loan = values.get(FIELD_MAP["loan_amount"], "").strip()
        fa_loan = fa_values.get(FA_FIELDS["first_loan_amount"], "").strip()
        if main_loan and fa_loan and _money_to_int(main_loan) != _money_to_int(fa_loan):
            issues.append({
                "severity": "blocker",
                "message": f"Section 3B financing amount ({main_loan}) doesn't match the 40-11 addendum's principal amount ({fa_loan})",
                "key": "loan_amount_mismatch",
            })

    # 2. Attachment consistency: the "Third Party Financing Addendum"
    # checkboxes on the main contract (Sec 3B row + Sec 22 addenda list)
    # should be checked if and only if a 40-11 is actually attached.
    checked_3b = _is_checked(values, FIELD_MAP["third_party_financing_3b"])
    checked_22 = _is_checked(values, FIELD_MAP["third_party_financing"])
    if has_addendum:
        if not checked_3b:
            issues.append({"severity": "blocker", "message": "Section 3B: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached", "key": "addendum_checkbox_mismatch"})
        if not checked_22:
            issues.append({"severity": "blocker", "message": "Section 22: Third Party Financing Addendum checkbox not checked, but a 40-11 addendum is attached", "key": "addendum_checkbox_mismatch"})
    else:
        if checked_3b:
            issues.append({"severity": "blocker", "message": "Section 3B: Third Party Financing Addendum checkbox is checked, but no 40-11 addendum is attached", "key": "addendum_checkbox_mismatch"})
        if checked_22:
            issues.append({"severity": "blocker", "message": "Section 22: Third Party Financing Addendum checkbox is checked, but no 40-11 addendum is attached", "key": "addendum_checkbox_mismatch"})

    # 3. Contract vs. Amendment: price. Only compared when the amendment's
    # OWN price-change section is actually in use (its checkbox checked
    # and a total filled in) -- an amendment used solely to change the
    # closing date legitimately leaves this section blank, and a blank
    # amendment price is not evidence of anything.
    if has_amendment:
        amend_price_checked = _is_checked(amend_values, AMEND_FIELDS["price_checkbox"])
        amend_price = amend_values.get(AMEND_FIELDS["price_total"], "").strip()
        main_price = values.get(FIELD_MAP["sales_price"], "").strip()
        if amend_price_checked and amend_price and main_price and _money_to_int(main_price) != _money_to_int(amend_price):
            issues.append({
                "severity": "blocker",
                "message": f"Sales Price mismatch: contract ({main_price}) vs. amendment ({amend_price})",
                "key": AMEND_PRICE_MISMATCH_KEY,
            })

        # 4. Contract vs. Amendment: property address, as a sanity check
        # that the amendment actually belongs to this contract -- not a
        # legal "conflict" the way a price mismatch is, so this stays a
        # warning even though a real mismatch here is a bigger problem in
        # practice (wrong file attached entirely).
        amend_addr = amend_values.get(AMEND_FIELDS["address"], "").strip()
        main_addr = values.get(FIELD_MAP["address"], "").strip()
        if amend_addr and main_addr and _normalized_address(amend_addr) != _normalized_address(main_addr):
            issues.append({
                "severity": "warning",
                "message": f"Amendment's property address ({amend_addr}) doesn't match the contract's ({main_addr}) -- confirm this amendment belongs to this file",
                "key": AMEND_ADDRESS_MISMATCH_KEY,
            })

    blocking_issues = [i for i in issues if i["severity"] == "blocker"]
    return {
        "recognized": True,
        "complete": len(blocking_issues) == 0,
        "issues": issues,
        "looks_like_blank_draft": core_missing / CORE_BLOCKING_FIELDS >= BLANK_DRAFT_THRESHOLD,
        "page_count": page_count,
        "has_addendum": has_addendum,
        "has_amendment": has_amendment,
        "severity": _severity_summary(issues),
        # As written on the form (Section 2A), for the report header and
        # email subject. Blank strings when the form left them blank.
        "property": {
            "address": values.get(FIELD_MAP["address"], "").strip(),
            "city": values.get(FIELD_MAP["city"], "").strip(),
            "county": values.get(FIELD_MAP["county"], "").strip(),
        },
    }


# --- Contract-vs-contract "WHAT CHANGED" diff -------------------------------
#
# Distinct from everything above: check_tc_file() audits ONE contract
# (optionally cross-checked against a DIFFERENT form -- a 40-11 or 39-11).
# compare_contracts() instead diffs TWO uploads of the SAME 20-19 template
# against each other -- e.g. a signed original vs. a later re-filled
# version. (key, display label, is_money) -- money fields are compared by
# parsed cents value via _money_to_int so formatting differences ($725,000
# vs $725,000.00) don't read as a change; text fields are compared
# case/whitespace-normalized for the same reason the address check in
# check_tc_file() had to be (a real value shouldn't read as "changed" just
# because two different tools capitalized or spaced it differently).
# Ordered financial fields first to match how a TC actually scans a diff.
COMPARE_FIELDS = [
    ("sales_price", "Sales Price", True),
    ("loan_amount", "Financing Amount", True),
    ("earnest_money_amount", "Earnest Money", True),
    ("option_fee_amount", "Option Fee", True),
    ("escrow_agent_name", "Escrow Agent", False),
    ("title_company", "Title Company", False),
    ("buyer_name", "Buyer", False),
    ("seller_name", "Seller", False),
    ("address", "Property Address", False),
    ("city", "City", False),
    ("county", "County", False),
]

# Closing date and option period are deliberately NOT in COMPARE_FIELDS and
# must never be reported as "unchanged" -- rendering 20-19_2.pdf with a
# marker (2026-09-04) confirmed neither blank has a backing AcroForm field
# at all in this template (this app itself only ever draws them via a
# reportlab overlay -- see pdf_filler.py's own comment listing both
# alongside the page-11 header as overlay-only). There is nothing to read
# back from ANY uploaded copy of this form for these two values, regardless
# of which tool filled it -- reporting "unchanged" would imply a
# verification that structurally cannot happen.
NOT_COMPARABLE_LABELS = ["Closing date", "Option period"]
NOT_COMPARABLE_REASON = "Not compared -- this value is not available as a readable form field."


def _normalized_text(raw: str) -> str:
    return " ".join(raw.strip().upper().split())


def compare_contracts(original_path: str, updated_path: str) -> dict:
    """Field-level diff between two TREC 20-19 contract uploads (e.g. an
    original signed contract and a later re-filled/updated version of the
    SAME form) -- as distinct from check_tc_file()'s contract-vs-a-
    DIFFERENT-form cross-checks (40-11, 39-11).

    original_path and updated_path are two SEPARATE, EXPLICIT slots, not a
    list to classify. Unlike a 40-11 or 39-11, which have their own distinct
    field-name fingerprint, two uploads of the same 20-19 template are
    fingerprint-identical -- there is no reliable way to infer which one is
    "the original" from field content (Effective Date, once filled, might
    seem like an ordering signal, but it's exactly the kind of inference
    this app avoids making elsewhere -- see check_tc_file()'s own docstring
    on not guessing at unverified relationships). Callers MUST know and
    supply the correct slot themselves.

    Only compares COMPARE_FIELDS -- fields already rect-verified in
    FIELD_MAP. Closing date and option period are never compared; see
    NOT_COMPARABLE_LABELS above for why.

    Returns {"recognized": bool, "changes": [...], "not_compared": [...],
    "not_compared_reason": str} on success, or {"recognized": False,
    "message": str} if either upload doesn't match the 20-19 template.
    Raises whatever pypdf raises on a file that isn't a readable PDF at all --
    callers should catch that and turn it into a 400, not a 500."""
    orig_values = _read_values(original_path)
    upd_values = _read_values(updated_path)

    unrecognized = []
    if _matched_main(orig_values) < MIN_MATCHED_FIELDS:
        unrecognized.append("original")
    if _matched_main(upd_values) < MIN_MATCHED_FIELDS:
        unrecognized.append("updated")
    if unrecognized:
        return {
            "recognized": False,
            "message": (
                f"The {' and '.join(unrecognized)} file"
                f"{'s' if len(unrecognized) > 1 else ''} didn't look like a "
                "TREC 20-19 form we recognize -- field names didn't match "
                "our template."
            ),
        }

    changes = []
    for key, label, is_money in COMPARE_FIELDS:
        field_name = FIELD_MAP[key]
        o_raw = orig_values.get(field_name, "").strip()
        u_raw = upd_values.get(field_name, "").strip()

        if not o_raw and not u_raw:
            changes.append({"field": label, "status": "missing", "key": key})
            continue

        if is_money:
            o_norm = _money_to_int(o_raw) if o_raw else None
            u_norm = _money_to_int(u_raw) if u_raw else None
        else:
            o_norm = _normalized_text(o_raw) if o_raw else None
            u_norm = _normalized_text(u_raw) if u_raw else None

        if o_norm == u_norm:
            changes.append({"field": label, "status": "unchanged", "key": key})
        else:
            changes.append({
                "field": label,
                "status": "changed",
                "key": key,
                "from": o_raw or "(blank)",
                "to": u_raw or "(blank)",
            })

    return {
        "recognized": True,
        "changes": changes,
        "not_compared": NOT_COMPARABLE_LABELS,
        "not_compared_reason": NOT_COMPARABLE_REASON,
    }
