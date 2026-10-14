"""
co_audit.py -- TC File Check for the Colorado Real Estate Commission's
CBS1 Contract to Buy and Sell Real Estate (Residential), the version
"for use on or after January 1, 2026" (Adoption Date August 5, 2025),
downloaded 2026-10-12 from dre.colorado.gov/real-estate-broker-contracts-and-forms.

Colorado is the closest analog to TREC: Commission-approved forms that
licensees must use (4 CCR 725-1, Rule 7), published as free fillable PDFs.
Unlike TREC's 20-19, the CBS1's AcroForm field names describe their own
role and match their tooltips -- e.g. "Inspection Objection Deadline" sits
in the Date or Deadline column of the §3.1 row with that label (rect-
checked 2026-10-12: the whole deadline column is x=395..579 on pages 3-4,
one row per field, in the printed table's order). FIELDS below only lists
names confirmed that way.

What makes a Colorado check worth running is §3.2 (Applicability of Terms):
"If any deadline in § 3.1. ... is left blank or completed with 'N/A', or the
word 'Deleted,' such deadline is not applicable and the corresponding
provision containing the deadline is deleted." A blank New Loan
Availability Deadline doesn't just look untidy -- it deletes the buyer's
§5.2.2 right to terminate if the loan falls through. Those findings are
warnings, not blockers: deleting a contingency is a legitimate deal choice
(a cash-strong offer), so the report names the consequence and asks the TC
to confirm it was intended -- same reasoning as TREC's blank option fee.

v1 scope: the CBS1 by itself. The foreclosure variant (CBSF1) uses
different field names and comes back unrecognized; other CREC forms in a
multi-file upload are ignored (not audited), and the result says so.
"""
import re
from datetime import date, timedelta
from pypdf import PdfReader
from pdf_validator import _read_values, _is_checked

FORM_LABEL = "Colorado CBS1"

# Raw AcroForm names on the CBS1 (2026 version).
F = {
    "date": "Date",
    "buyer": "Buyer Name",
    "seller": "Seller Name",
    "county": "County",
    "legal": "Property Legal Description",
    "street": "Street Address",
    "city": "City",
    "state": "State",
    "zip": "Zip Code",
    "price": "Purchase Price",
    "earnest": "Earnest Money",
    "new_loan": "New Loan",
    "assumption": "Assumption Balance",
    "private": "Private Balance",
    "seller_fin": "Seller Financing",
    "cash": "Cash At Closing",
    "total_left": "Total 01",
    "total_right": "Total 02",
    "earnest_holder": "Earnest Money Holder",
    "earnest_form": "Earnest Money Form",
    "closing": "Closing Date",
    "possession_date": "Possession Date",
    "possession_time": "Possession Time",
    "acceptance_date": "Acceptance Deadline Date",
    "acceptance_time": "Acceptance Deadline Time",
}

# Names only the CBS1 carries (not TREC's, not the CBSF1's) -- presence of
# the field name, filled or not, is what identifies the template.
FINGERPRINT = ["Earnest Money Holder", "Total 02", "Record Title Objection Deadline",
               "New Loan Availability Deadline", "Inspection Objection Deadline",
               "Seller Selects Title Checkbox", "Available Funds Does Checkbox",
               "Buyer Name", "County"]
MIN_MATCHED = 6

# §3.1 rows: (field name, item no., label as printed).
DEADLINES = {
    "record_title": ("Record Title Deadline and Tax Certificate", 3, "Record Title Deadline"),
    "record_title_obj": ("Record Title Objection Deadline", 4, "Record Title Objection Deadline"),
    "off_record": ("Off Record Title Deadline", 5, "Off-Record Title Deadline"),
    "off_record_obj": ("Off Record Title Objection Deadline", 6, "Off-Record Title Objection Deadline"),
    "title_resolution": ("Title Resolution Deadline", 7, "Title Resolution Deadline"),
    "assoc_docs": ("Association Documents Deadline", 9, "Association Documents Deadline"),
    "assoc_term": ("Association Documents Termination Deadline", 10, "Association Documents Termination Deadline"),
    "spd": ("Sellers Property Disclosure Deadline", 11, "Seller's Property Disclosure Deadline"),
    "loan_app": ("New Loan Application Deadline", 13, "New Loan Application Deadline"),
    "loan_terms": ("New Loan Terms Deadline", 14, "New Loan Terms Deadline"),
    "loan_avail": ("New Loan Availability Deadline", 15, "New Loan Availability Deadline"),
    "credit": ("Buyers Credit Information Deadline", 16, "Buyer's Credit Information Deadline"),
    "credit_disapproval": ("Disapproval of Buyers Credit Information Deadline", 17, "Disapproval of Buyer's Credit Information Deadline"),
    "existing_loan": ("Existing Loan Deadline", 18, "Existing Loan Deadline"),
    "existing_loan_term": ("Existing Loan Termination Deadline", 19, "Existing Loan Termination Deadline"),
    "loan_transfer": ("Loan Transfer Approval Deadline", 20, "Loan Transfer Approval Deadline"),
    "seller_fin": ("Seller or Private Financing Deadline", 21, "Seller or Private Financing Deadline"),
    "appraisal": ("Appraisal Deadline", 22, "Appraisal Deadline"),
    "appraisal_obj": ("Appraisal Objection Deadline", 23, "Appraisal Objection Deadline"),
    "appraisal_res": ("Appraisal Resolution Deadline", 24, "Appraisal Resolution Deadline"),
    "ilc": ("New ILC or New Survey Deadline", 25, "New ILC or New Survey Deadline"),
    "ilc_obj": ("New ILC or New Survey Objection Deadline", 26, "New ILC or New Survey Objection Deadline"),
    "ilc_res": ("New ILC or New Survey Resolution Deadline", 27, "New ILC or New Survey Resolution Deadline"),
    "insp_term": ("Inspection Termination Deadline", 30, "Inspection Termination Deadline"),
    "insp_obj": ("Inspection Objection Deadline", 31, "Inspection Objection Deadline"),
    "insp_res": ("Inspection Resolution Deadline", 32, "Inspection Resolution Deadline"),
    "insurance_term": ("Property Insurance Termination Deadline", 33, "Property Insurance Termination Deadline"),
    "dd_delivery": ("Due Diligence Documents Delivery Deadline", 34, "Due Diligence Documents Delivery Deadline"),
    "dd_obj": ("Due Diligence Documents Objection Deadline", 35, "Due Diligence Documents Objection Deadline"),
    "dd_res": ("Due Diligence Documents Resolution Deadline", 36, "Due Diligence Documents Resolution Deadline"),
    "closing": ("Closing Date", 39, "Closing Date"),
    "possession": ("Possession Date", 40, "Possession Date"),
    "acceptance": ("Acceptance Deadline Date", 42, "Acceptance Deadline Date"),
}

# Each chain must run in order (delivery <= objection <= resolution). An
# objection deadline before its delivery deadline leaves the buyer objecting
# to documents they aren't owed yet.
SEQUENCES = [
    ["record_title", "record_title_obj", "title_resolution"],
    ["off_record", "off_record_obj", "title_resolution"],
    ["assoc_docs", "assoc_term"],
    ["loan_app", "loan_terms", "loan_avail"],
    ["credit", "credit_disapproval"],
    ["existing_loan", "existing_loan_term"],
    ["appraisal", "appraisal_obj", "appraisal_res"],
    ["ilc", "ilc_obj", "ilc_res"],
    ["insp_obj", "insp_res"],
    ["dd_delivery", "dd_obj", "dd_res"],
]
# Not compared against Closing Date: possession can follow closing, and
# acceptance has its own check.
NOT_BEFORE_CLOSING = {"closing", "possession", "acceptance"}

LOAN_BOXES = [("Conventional", "Conventional Checkbox"), ("FHA", "FHA Checkbox"), ("VA", "VA Checkbox"),
              ("Bond", "Bond Checkbox"), ("Other", "Other Checkbox")]

# Two-way "check one" choices -- both checked contradicts itself.
EXCLUSIVE_PAIRS = [
    ("§3.3.3 holiday extension", "Deadlines Will Checkbox", "Deadlines Will Not Checkbox"),
    ("§2.5.2 encumbered inclusions", "Encumbered Inclusion Will Checkbox", "Encumbered Inclusion Will Not Checkbox"),
    ("§2.5.4 leased items", "Leased Items Will Checkbox", "Leased Items Will Not Checkbox"),
    ("§2.5.5 solar power plan", "Solar Power Will Checkbox", "Solar Power Will Not Checkbox"),
    ("§4.4.3 Available Funds", "Available Funds Does Checkbox", "Available Funds Does Not Checkbox"),
    ("§4.6 release of liability", "Assumption Seller Will Checkbox", "Assumption Seller Will Not Checkbox"),
    ("§8.1 title company selection", "Seller Selects Title Checkbox", "Buyer Selects Title Checkbox"),
    ("§8.1 OEC", "OEC Will Checkbox", "OEC Will Not Checkbox"),
    ("§6.2.3 cost of appraisal", "Cost of Appraisal Buyer Checkbox", "Cost of Appraisal Seller Checkbox"),
    ("§10.10 source of potable water", "Source of Potable Water Does Checkbox", "Source of Potable Water Does Not Checkbox"),
    ("§14 closing instructions", "Closing Instructions Are Checkbox", "Closing Instructions Are Not Checkbox"),
    ("Buyer's broker earnest money receipt", "Broker Working With Buyer Does Checkbox", "Broker Working With Buyer Does Not Checkbox"),
    ("Seller's broker earnest money receipt", "Broker Working With Seller Does Checkbox", "Broker Working With Seller Does Not Checkbox"),
]

# Who-pays rows in §15 -- one box per row.
PAYER_GROUPS = [
    ("§15.1 Closing Services Fee", ["Closing Services Fee Buyer Checkbox", "Closing Services Fee Seller Checkbox",
                                    "Closing Services Fee 50 50 Checkbox", "Closing Services Fee Other Checkbox"]),
    ("§15.2.1 Record Change Fee", ["Record Change Fee Buyer Checkbox", "Record Change Fee Seller Checkbox",
                                   "Record Change Fee 50 50 Checkbox", "Record Change Fee Not Applicable Checkbox"]),
    ("§15.2.3 Reserves/Working Capital", ["Reserves and Working Capital Buyer Checklist", "Reserves and Working Capital Seller Checklist",
                                          "Reserves and Working Capital 50 50 Checklist", "Reserves and Working Capital Not Applicable Checklist"]),
    ("§15.2.4 Other Fees", ["Other Fees Buyer Checklist", "Other Fees Seller Checklist",
                            "Other Fees 50 50 Checklist", "Other Fees Not Applicable Checklist"]),
    ("§15.3 Local Transfer Tax", ["Local Transfer Tax Buyer Checklist", "Local Transfer Tax Seller Checklist",
                                  "Local Transfer Tax 50 50 Checklist", "Local Transfer Tax Not Applicable Checklist"]),
    ("§15.4 Sales and Use Tax", ["Sales and Use Tax Buyer Checklist", "Sales and Use Tax Seller Checklist",
                                 "Sales and Use Tax 50 50 Checklist", "Sales and Use Tax Not Applicable Checklist"]),
    ("§15.5 Private Transfer Fee", ["Private Transfer Fee Buyer Checkbox", "Private Transfer Fee Seller Checkbox",
                                    "Private Transfer Fee 50 50 Checkbox", "Private Transfer Fee Not Applicable Checkbox"]),
    ("§15.7 Utility Transfer Fees", ["Utility Transfer Fee Buyer Checkbox", "Utility Transfer Fee Seller Checkbox",
                                     "Utility Transfer Fee 50 50 Checkbox", "Utility Transfer Fee Not Applicable  Checkbox"]),
]

_NA = re.compile(r"^\s*(n\s*/?\s*a|deleted|none)\s*\.?\s*$", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_PROPERTY_STATE_OK = {"CO", "COLO", "COLORADO"}


def _nth_weekday(year, month, weekday, n):
    """n-th (1-based) weekday of a month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def holidays(year: int) -> dict:
    """§3.3.3 "federal or Colorado state holiday": {date: name}, with the
    federal observed-day rule (Saturday -> Friday, Sunday -> Monday) for the
    fixed-date ones. Cabrini Day (first Monday in October) is Colorado's."""
    fixed = {date(year, 1, 1): "New Year's Day", date(year, 6, 19): "Juneteenth",
             date(year, 7, 4): "Independence Day", date(year, 11, 11): "Veterans Day",
             date(year, 12, 25): "Christmas Day"}
    out = {}
    for d, name in fixed.items():
        out[d] = name
        if d.weekday() == 5:
            out[d - timedelta(days=1)] = name + " (observed)"
        elif d.weekday() == 6:
            out[d + timedelta(days=1)] = name + " (observed)"
    out.update({
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Presidents' Day",
        _nth_weekday(year, 5, 0, -1): "Memorial Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 1): "Cabrini Day (Colorado)",
        _nth_weekday(year, 10, 0, 2): "Columbus Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving",
    })
    return out


def holiday_name(d: date):
    # Next year's table too: a Saturday Jan 1 is observed on Fri Dec 31 of
    # the year before (e.g. 2027-12-31 for New Year's Day 2028), and
    # holidays(2027) alone never sees it. Found by txo-trec-qa 2026-10-13.
    return holidays(d.year).get(d) or (holidays(d.year + 1).get(d) if d.month == 12 else None)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and holiday_name(d) is None


def _v(values: dict, name: str) -> str:
    return (values.get(name) or "").strip()


def _blank_or_na(text: str) -> bool:
    """§3.2: blank, "N/A" and "Deleted" all mean the same thing."""
    return not text or bool(_NA.match(text))


def _money(text: str):
    if not text or _NA.match(text):
        return None
    cleaned = re.sub(r"[^\d.]", "", text)
    try:
        return float(cleaned) if cleaned and cleaned != "." else None
    except ValueError:
        return None


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}".replace(".00", "")


def _parse_date(text: str):
    """Absolute dates only. Relative entries ("MEC + 10 days") and times
    return None and are left out of the date-order checks rather than
    guessed at."""
    t = (text or "").strip()
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2}|\d{4})$", t)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y = y + 2000 if y < 100 else y
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$", t)
            if not m or m.group(1)[:3].lower() not in MONTHS:
                return None
            mo, d, y = MONTHS[m.group(1)[:3].lower()], int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def matched(values: dict) -> int:
    return sum(1 for name in FINGERPRINT if name in values)


def is_cbs1(values: dict) -> bool:
    return matched(values) >= MIN_MATCHED


def _issue(severity: str, message: str, key: str) -> dict:
    return {"severity": severity, "message": message, "key": key}


def _deadline(values: dict, key: str):
    name, item, label = DEADLINES[key]
    raw = _v(values, name)
    return raw, _parse_date(raw), item, label


def audit_values(values: dict) -> list:
    issues = []

    # --- Property and parties (§2) -------------------------------------
    for key, msg, sev in [
        ("street", "§2.4: Property street address is blank", "blocker"),
        ("city", "§2.4: City is blank", "blocker"),
        ("county", "§2.4: County is blank -- the title commitment and deed need it", "blocker"),
        ("legal", "§2.4: Legal description is blank -- title will ask for it before closing", "warning"),
        ("buyer", "§2.1: Buyer name is blank", "warning"),
        ("seller", "§2.3: Seller name is blank", "warning"),
    ]:
        if not _v(values, F[key]):
            issues.append(_issue(sev, msg, f"co_{key}_blank"))
    state = _v(values, F["state"]).upper().replace(".", "")
    if state and state not in _PROPERTY_STATE_OK:
        issues.append(_issue("blocker", f"§2.4: Property state is \"{_v(values, F['state'])}\" -- the CBS1 is only for Colorado property",
                             "co_state_not_colorado"))

    # --- Price and terms (§4.1) -----------------------------------------
    price = _money(_v(values, F["price"]))
    if price is None:
        issues.append(_issue("blocker", "§4.1: Purchase Price is blank", "co_price_blank"))
    earnest = _money(_v(values, F["earnest"]))
    if earnest is None:
        issues.append(_issue("blocker", "§4.1 / §4.3: Earnest Money amount is blank", "co_earnest_blank"))
    if not _v(values, F["earnest_holder"]):
        issues.append(_issue("blocker", "§4.3: Earnest Money Holder is blank -- no one is named to hold the deposit", "co_earnest_holder_blank"))

    right_col = {k: _money(_v(values, F[k])) for k in ("earnest", "new_loan", "assumption", "private", "seller_fin", "cash")}
    filled = [x for x in right_col.values() if x is not None]
    total_right = _money(_v(values, F["total_right"]))
    total_left = _money(_v(values, F["total_left"]))
    if price is not None and filled:
        s = sum(filled)
        if abs(s - price) > 0.5:
            issues.append(_issue("blocker",
                f"§4.1: Earnest Money + loan + Cash at Closing add up to {_fmt_money(s)}, not the {_fmt_money(price)} Purchase Price",
                "co_price_math"))
        elif total_right is not None and abs(total_right - s) > 0.5:
            issues.append(_issue("warning", f"§4.1: TOTAL (right column) says {_fmt_money(total_right)}, but the items above it add up to {_fmt_money(s)}",
                                 "co_total_right"))
    if price is not None and total_left is not None and abs(total_left - price) > 0.5:
        issues.append(_issue("warning", f"§4.1: TOTAL (left column) says {_fmt_money(total_left)}, but the Purchase Price is {_fmt_money(price)}",
                             "co_total_left"))

    # --- Financing (§4.5, §5) --------------------------------------------
    new_loan = right_col["new_loan"] or 0
    loan_types = [label for label, box in LOAN_BOXES if _is_checked(values, box)]
    if new_loan > 0 and not loan_types:
        issues.append(_issue("blocker", "§4.5.3: a New Loan amount is listed, but no loan type (Conventional/FHA/VA/Bond/Other) is checked",
                             "co_loan_type_missing"))
    if loan_types and new_loan == 0:
        issues.append(_issue("warning", f"§4.5.3: {'/'.join(loan_types)} is checked, but §4.1 shows no New Loan amount",
                             "co_loan_amount_missing"))
    if new_loan > 0:
        for key, consequence in [
            ("loan_avail", "Buyer's §5.2.2 right to terminate if the loan isn't approved is deleted"),
            ("loan_terms", "Buyer's §5.2.1 right to terminate over loan terms is deleted"),
        ]:
            raw, _, item, label = _deadline(values, key)
            if _blank_or_na(raw):
                issues.append(_issue("warning", f"§3.1 item {item}: {label} is {'blank' if not raw else raw} with a New Loan in §4.1 -- "
                                                f"under §3.2 that means {consequence}. Confirm that's intended.", f"co_{key}_deleted"))
        if _blank_or_na(_v(values, DEADLINES["appraisal_obj"][0])) and not set(loan_types) & {"FHA", "VA"}:
            issues.append(_issue("warning", "§3.1 item 23: Appraisal Objection Deadline is blank -- under §3.2 the buyer has no appraisal objection right. "
                                            "Confirm that's intended.", "co_appraisal_deleted"))
    if (right_col["seller_fin"] or right_col["private"] or 0) > 0 and _blank_or_na(_v(values, DEADLINES["seller_fin"][0])):
        issues.append(_issue("warning", "§3.1 item 21: Seller or Private Financing Deadline is blank, but §4.1 lists seller/private financing",
                             "co_seller_fin_deadline_blank"))
    if (right_col["assumption"] or 0) > 0 and _blank_or_na(_v(values, DEADLINES["loan_transfer"][0])):
        issues.append(_issue("warning", "§3.1 item 20: Loan Transfer Approval Deadline is blank, but §4.1 lists an Assumption Balance",
                             "co_assumption_deadline_blank"))
    if not (_is_checked(values, "Available Funds Does Checkbox") or _is_checked(values, "Available Funds Does Not Checkbox")):
        issues.append(_issue("warning", "§4.4.3: neither Does nor Does Not is checked for Available Funds", "co_available_funds_unchecked"))

    # --- Contingencies deleted by a blank deadline (§3.2) ----------------
    insp_obj, insp_term = _v(values, DEADLINES["insp_obj"][0]), _v(values, DEADLINES["insp_term"][0])
    if _blank_or_na(insp_obj) and _blank_or_na(insp_term):
        issues.append(_issue("warning", "§3.1 items 30-31: Inspection Objection and Inspection Termination Deadlines are both blank -- "
                                        "under §3.2 the buyer has no inspection right. Confirm that's intended.", "co_inspection_deleted"))
    if _blank_or_na(_v(values, DEADLINES["record_title_obj"][0])):
        issues.append(_issue("warning", "§3.1 item 4: Record Title Objection Deadline is blank -- under §3.2 the buyer can't object to the title commitment",
                             "co_title_objection_deleted"))

    # --- Closing, possession, acceptance ---------------------------------
    closing_raw, closing, _, _ = _deadline(values, "closing")
    if _blank_or_na(closing_raw):
        issues.append(_issue("blocker", "§3.1 item 39: Closing Date is blank", "co_closing_blank"))
    elif closing and closing.weekday() >= 5:
        issues.append(_issue("warning", f"§3.1 item 39: Closing Date ({closing_raw}) is a {closing.strftime('%A')} -- confirm the closing company is open",
                             "co_closing_weekend"))
    elif closing and holiday_name(closing):
        issues.append(_issue("warning", f"§3.1 item 39: Closing Date ({closing_raw}) is {holiday_name(closing)} -- banks may not move "
                                        "wire funds; confirm the closing company and lender can fund", "co_closing_holiday"))
    if _blank_or_na(_v(values, F["possession_date"])) or _blank_or_na(_v(values, F["possession_time"])):
        issues.append(_issue("warning", "§3.1 items 40-41: Possession Date or Possession Time is blank", "co_possession_blank"))
    acc_raw, acc, _, _ = _deadline(values, "acceptance")
    if _blank_or_na(acc_raw):
        issues.append(_issue("warning", "§3.1 item 42: Acceptance Deadline Date is blank -- under §27 the offer has no stated expiration", "co_acceptance_blank"))
    _, poss, _, _ = _deadline(values, "possession")
    if closing and poss and poss < closing:
        issues.append(_issue("warning", f"§17: Possession Date ({poss:%m/%d/%Y}) is before the Closing Date ({closing:%m/%d/%Y}) -- "
                                        "early possession usually needs a separate agreement", "co_possession_before_closing"))
    if closing and acc and acc > closing:
        issues.append(_issue("blocker", f"§27: Acceptance Deadline ({acc:%m/%d/%Y}) is after the Closing Date ({closing:%m/%d/%Y})",
                             "co_acceptance_after_closing"))

    # --- Deadline order ----------------------------------------------------
    for chain in SEQUENCES:
        dated = [(k, _deadline(values, k)) for k in chain]
        dated = [(k, d[1], d[2], d[3]) for k, d in dated if d[1]]
        for (k1, d1, i1, l1), (k2, d2, i2, l2) in zip(dated, dated[1:]):
            if d2 < d1:
                issues.append(_issue("blocker", f"§3.1: {l2} (item {i2}, {d2:%m/%d/%Y}) is before {l1} (item {i1}, {d1:%m/%d/%Y})",
                                     f"co_order_{k1}_{k2}"))
    if closing:
        for key, (name, item, label) in DEADLINES.items():
            if key in NOT_BEFORE_CLOSING:
                continue
            d = _parse_date(_v(values, name))
            if d and d > closing:
                issues.append(_issue("blocker", f"§3.1 item {item}: {label} ({d:%m/%d/%Y}) is after the Closing Date ({closing:%m/%d/%Y})",
                                     f"co_after_closing_{key}"))

    # --- Check-one conflicts ---------------------------------------------
    for label, a, b in EXCLUSIVE_PAIRS:
        if _is_checked(values, a) and _is_checked(values, b):
            issues.append(_issue("blocker", f"{label}: both boxes are checked", f"co_conflict_{a}"))
    for label, boxes in PAYER_GROUPS:
        if sum(1 for b in boxes if _is_checked(values, b)) > 1:
            issues.append(_issue("blocker", f"{label}: more than one payer is checked", f"co_conflict_{boxes[0]}"))
    if not (_is_checked(values, "Seller Selects Title Checkbox") or _is_checked(values, "Buyer Selects Title Checkbox")):
        issues.append(_issue("warning", "§8.1: neither Seller Selects nor Buyer Selects Title Insurance Company is checked", "co_title_selection_unchecked"))

    # --- §29 buyer's brokerage compensation -------------------------------
    for n in ("1", "2", "3"):
        # Fully qualified names -- these three rows are kids of a "29" parent field.
        if _is_checked(values, f"29.{n} Checkbox") and not (_v(values, f"29.{n} Percentage") or _v(values, f"29.{n} Amount")):
            issues.append(_issue("warning", f"§29: compensation line {n} is checked, but both the % and $ amount are blank", f"co_comp_{n}_blank"))

    # Federal lead-based paint disclosure applies to homes built before 1978;
    # "N/A" means someone decided, a bare blank may mean nobody looked.
    if not _v(values, "Lead Based Paint Disclosure Deadline") and not _v(values, "LeadBased Paint Termination Deadline"):
        issues.append(_issue("warning", "§3.1 items 12 and 38: Lead-Based Paint deadlines are blank -- required if the home "
                                        "was built before 1978, otherwise write N/A", "co_lead_paint_blank"))

    if not _v(values, F["date"]):
        issues.append(_issue("warning", "Page 1: contract Date is blank", "co_date_blank"))
    return issues


def check_co_file(pdf_paths, page_count=None, all_values=None, summary=None) -> dict:
    """Same result shape as tc_audit.check_tc_file. `summary` is
    tc_audit._severity_summary, passed in to avoid a circular import. Up to
    4 files: the CBS1 plus any Amend/Extend, Counterproposal, Inspection
    Objection Notice, Source of Water or Residential Addendum, each
    cross-checked against it by co_forms."""
    import co_forms
    if all_values is None:
        all_values = [_read_values(p) for p in pdf_paths]
    if page_count is None:
        page_count = sum(len(PdfReader(p).pages) for p in pdf_paths)
    values = next(v for v in all_values if is_cbs1(v))
    issues = audit_values(values)
    others, unknown, seen_contract = [], 0, False
    for v in all_values:
        if v is values and not seen_contract:
            seen_contract = True
            continue
        kind = co_forms.classify(v)
        if kind and kind != "cbs1":
            others.append((kind, v))
        else:
            unknown += 1
    issues += co_forms.cross_check(values, others)
    if unknown:
        issues.append(_issue("warning",
                             f"{unknown} attached file{'s' if unknown != 1 else ''} wasn't recognized as a Colorado form "
                             f"this check reads -- not checked", "co_other_files_not_checked"))
    blockers = [i for i in issues if i["severity"] == "blocker"]
    core = ["street", "city", "county", "price", "earnest", "earnest_holder", "closing"]
    core_missing = sum(1 for k in core if not _v(values, F[k]))
    return {
        "recognized": True,
        "form": FORM_LABEL,
        "state": "CO",
        "complete": not blockers,
        "issues": issues,
        "looks_like_blank_draft": core_missing / len(core) >= 0.7,
        "page_count": page_count,
        "has_addendum": False,
        "has_amendment": any(k == "ae41" for k, _ in others),
        "has_loan_assumption": False,
        "co_attachments": [co_forms.FORM_NAMES[k] for k, _ in others],
        "severity": summary(issues) if summary else None,
        "property": {
            "address": _v(values, F["street"]),
            "city": _v(values, F["city"]),
            "county": _v(values, F["county"]),
        },
    }


# --- Contract dates for the transaction side (tasks, Day One, reminders) ---
# Stage and plain-English action for each §3.1 row a TC works toward.
# Rows not listed here (e.g. Third Party Right) still appear on the
# contract; they just don't get a task.
DEADLINE_TASKS = {
    "record_title": ("Title", "title commitment and tax certificate due"),
    "record_title_obj": ("Title", "last day to object to the title commitment"),
    "off_record": ("Title", "off-record title documents due"),
    "off_record_obj": ("Title", "last day to object to off-record matters"),
    "title_resolution": ("Title", "title objections resolved or contract can terminate"),
    "assoc_docs": ("Title", "association documents due"),
    "assoc_term": ("Title", "last day to terminate over association documents"),
    "spd": ("Disclosures", "Seller's Property Disclosure due"),
    "loan_app": ("Financing", "buyer's loan application submitted"),
    "loan_terms": ("Financing", "last day to terminate over loan terms"),
    "loan_avail": ("Financing", "last day to terminate if the loan isn't available"),
    "credit": ("Financing", "buyer's credit information due to seller"),
    "credit_disapproval": ("Financing", "seller's last day to disapprove buyer's credit"),
    "existing_loan": ("Financing", "existing loan documents due"),
    "existing_loan_term": ("Financing", "last day to terminate over the existing loan"),
    "loan_transfer": ("Financing", "loan transfer approval due"),
    "seller_fin": ("Financing", "seller/private financing documents due"),
    "appraisal": ("Appraisal", "appraisal due"),
    "appraisal_obj": ("Appraisal", "last day to object to the appraisal"),
    "appraisal_res": ("Appraisal", "appraisal objection resolved"),
    "ilc": ("Inspections", "new ILC or survey due"),
    "ilc_obj": ("Inspections", "last day to object to the ILC or survey"),
    "ilc_res": ("Inspections", "ILC/survey objection resolved"),
    "insp_obj": ("Inspections", "last day to send the inspection objection"),
    "insp_term": ("Inspections", "last day to terminate over the inspection"),
    "insp_res": ("Inspections", "inspection objection resolved"),
    "insurance_term": ("Inspections", "last day to terminate over insurability"),
    "dd_delivery": ("Inspections", "due diligence documents due"),
    "dd_obj": ("Inspections", "last day to object to due diligence documents"),
    "dd_res": ("Inspections", "due diligence objection resolved"),
}

# The deadlines worth a reminder text the day before: missing one of these
# costs the buyer a right to terminate (§3.2 / §24).
REMINDER_KEYS = ("insp_obj", "loan_avail", "appraisal_obj", "record_title_obj")


def contract_deadlines(values: dict) -> list:
    """[{key, item, label, raw, date, stage, action}] for every §3.1 row
    in DEADLINE_TASKS that the contract actually fills in. "N/A"/blank rows
    are left out (§3.2: that provision is deleted). `date` is None when the
    entry isn't an absolute date (e.g. "MEC + 7") -- the raw text is kept so
    nothing is guessed."""
    out = []
    for key, (stage, action) in DEADLINE_TASKS.items():
        name, item, label = DEADLINES[key]
        raw = _v(values, name)
        if _blank_or_na(raw):
            continue
        out.append({"key": key, "item": item, "label": label, "raw": raw, "date": _parse_date(raw),
                    "stage": stage, "action": action})
    return out


def contract_key_dates(values: dict) -> dict:
    """Closing, acceptance and possession as written, parsed when absolute."""
    return {k: _parse_date(_v(values, DEADLINES[k][0])) for k in ("closing", "acceptance", "possession")}


def read_contract(pdf_path: str):
    """Values from a generated CBS1, or None if it isn't one/unreadable."""
    try:
        values = _read_values(pdf_path)
    except Exception:
        return None
    return values if is_cbs1(values) else None
