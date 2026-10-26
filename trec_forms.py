"""
trec_forms.py -- which TREC form (and which edition) an uploaded PDF is,
read from the form's own printed footer/header text, not its field names.

Why (2026-10-25): TC Check used to decide "this is a 20-19" purely by
AcroForm field names. TREC builds its fillable PDFs with the same export
tool, so a blank 9-18 Unimproved Property or 25-17 Farm and Ranch contract
matched the 20-19 fingerprint and got a full 20-19 report -- findings
written for 20-19 paragraphs and rects that don't exist on those forms.
An outdated 20-18 got the same treatment. The printed "TREC NO. X-Y" stamp
and the MM-DD-YYYY edition stamp are on every page of every TREC form, so
they're the honest signal for "which form is this".

Text only, so a scan (no text layer) detects as nothing and the caller
falls back to its old behavior. A flattened e-sign export still has its
text, so it detects fine even though its fields can't be read.

CURRENT_FORMS must match TREC. Every row was checked 2026-10-25 against
trec.texas.gov/sites/default/files/pdf-forms/<num>.pdf (form number and
edition stamp, and the next number up 404s) and, for the contracts, the
form's TREC page ("Form ID: 9-18, Effective Date: 07/01/2026"). Keep it in
sync with TREC_FORMS in ~/.claude/skills/trec-cbs1-form-currency/
check_trec_forms.py; when TREC replaces a form, change both in one commit.
"""
import re
from collections import Counter
from datetime import date

from pypdf import PdfReader

import filecrypt

# form id -> (title as printed on the form, current edition stamp, kind)
# kind "contract" = a primary contract; anything else rides with one.
CURRENT_FORMS = {
    "20-19": ("One to Four Family Residential Contract (Resale)", "05-04-2026", "contract"),
    "9-18": ("Unimproved Property Contract", "05-04-2026", "contract"),
    "25-17": ("Farm and Ranch Contract", "05-04-2026", "contract"),
    "23-20": ("New Home Contract (Incomplete Construction)", "05-04-2026", "contract"),
    "24-20": ("New Home Contract (Completed Construction)", "05-04-2026", "contract"),
    "30-18": ("Residential Condominium Contract (Resale)", "05-04-2026", "contract"),
    "11-9": ('Addendum for "Back-Up" Contract', "05-04-2026", "addendum"),
    "62-0": ('Seller\'s Notice to Buyer of Removal of Contingency Under Addendum for "Back-Up" Contract',
             "05-04-2026", "notice"),
    "36-11": ("Addendum for Property Subject to Mandatory Membership in a Property Owners Association",
              "05-04-2026", "addendum"),
    "39-11": ("Amendment to Contract", "05-04-2026", "amendment"),
    "40-11": ("Third Party Financing Addendum", "11-04-2024", "addendum"),
    "41-3": ("Loan Assumption Addendum", "11-07-2022", "addendum"),
    "61-0": ("Seller's Disclosure About Groundwater and Surface Water Rights", "05-04-2026", "notice"),
}
# An outdated form is any LOWER version of a current form's number (TREC
# keeps the base number and bumps the suffix: 20-18 -> 20-19). Not a
# hardcoded list on purpose. Each current form's own footer names the one
# it replaced ("This form replaces TREC NO. 20-18."), checked 2026-10-25:
# 20-18, 9-17, 25-16, 23-19, 24-19, 30-17, 11-8, 36-10, 39-10, 40-10, 41-2.
# Older ones are still served by TREC too (20-17, 9-16, 25-15, 40-9, 36-9).

# "TREC NO. 9-18" / "TREC No. 9 -18" (pypdf sometimes splits the number).
_NUM_RE = re.compile(r"TREC\s*NO\.?\s*(\d{1,2})\s*-\s*(\d{1,2})\b", re.I)
# The footer's "This form replaces TREC NO. 20-18." names the OLD form;
# a match inside it must not count as the form itself.
_REPLACES_RE = re.compile(r"replaces\s*$", re.I)
_DATE_RE = re.compile(r"\b(\d{2})\s*-\s*(\d{2})\s*-\s*(\d{4})\b")


def _page_forms(text: str) -> list:
    out = []
    for m in _NUM_RE.finditer(text):
        if _REPLACES_RE.search(text[max(0, m.start() - 20):m.start()]):
            continue
        out.append(f"{int(m.group(1))}-{int(m.group(2))}")
    return out


def _edition(dates: list):
    """Most common MM-DD-YYYY on the form's pages: the edition stamp is
    printed on every page, a date someone typed into a blank is not."""
    if not dates:
        return None
    return Counter(dates).most_common(1)[0][0]


def detect_forms(pdf_path) -> list:
    """Every TREC form found in one file, as [(form_id, edition)], the form
    with the most pages first (the contract in a merged packet). Edition is
    "MM-DD-YYYY" or None. [] when no TREC stamp is readable (a scan)."""
    pages, dates = Counter(), {}
    order = []
    for page in PdfReader(filecrypt.pdf_source(pdf_path)).pages:
        text = page.extract_text() or ""
        forms = _page_forms(text)
        if not forms:
            continue
        # One form per page: the one stamped most often on it.
        form = Counter(forms).most_common(1)[0][0]
        if form not in pages:
            order.append(form)
        pages[form] += 1
        dates.setdefault(form, []).extend("-".join(d) for d in _DATE_RE.findall(text))
    ranked = sorted(order, key=lambda f: (-pages[f], order.index(f)))
    return [(f, _edition(dates.get(f, []))) for f in ranked]


def detect_form(pdf_path) -> tuple:
    """(form_id, edition) of the file's main form -- a contract if the file
    has one, else whichever TREC form has the most pages. (None, None) when
    nothing is readable."""
    found = detect_forms(pdf_path)
    if not found:
        return None, None
    contracts = [f for f in found if kind(f[0]) == "contract"]
    return (contracts or found)[0]


def _split(form_id: str):
    base, ver = form_id.split("-")
    return base, int(ver)


def current_for(form_id: str):
    """The current form id with the same base number ("20-18" -> "20-19"),
    or None for a form number TC Check doesn't know."""
    base, _ = _split(form_id)
    for cur in CURRENT_FORMS:
        if _split(cur)[0] == base:
            return cur
    return None


def kind(form_id: str):
    cur = current_for(form_id)
    return CURRENT_FORMS[cur][2] if cur else None


def _date_key(edition: str):
    mm, dd, yyyy = edition.split("-")
    return (int(yyyy), int(mm), int(dd))


def status(form_id: str, edition=None) -> str:
    """"current", "outdated", "newer" (a number past what TC Check knows)
    or "unknown" (not a form in CURRENT_FORMS' families). An edition stamp
    older than the current one on the current number is "outdated" too; an
    unreadable stamp is not evidence of anything."""
    cur = current_for(form_id)
    if not cur:
        return "unknown"
    have, want = _split(form_id)[1], _split(cur)[1]
    if have < want:
        return "outdated"
    if have > want:
        return "newer"
    if edition and _date_key(edition) < _date_key(CURRENT_FORMS[cur][1]):
        return "outdated"
    return "current"


def label(form_id: str) -> str:
    """'TREC 9-18', for the report's "form" line."""
    return f"TREC {form_id}"


def outdated_message(form_id: str, edition=None) -> str:
    cur = current_for(form_id)
    title, cur_edition, _ = CURRENT_FORMS[cur]
    have = form_id + (f", {edition}" if edition else "")
    return f"This is an outdated TREC form ({have}); the current form is TREC {cur} {title} ({cur_edition})."


# Mandatory-use date of each current main contract: "Effective Date:
# 07/01/2026" on its trec.texas.gov form page, checked 2026-10-25 for all six.
MANDATORY_SINCE = {"20-19": "07/01/2026", "9-18": "07/01/2026", "25-17": "07/01/2026",
                   "23-20": "07/01/2026", "24-20": "07/01/2026", "30-18": "07/01/2026"}


def signed_before_mandatory(form_id: str, effective) -> bool:
    """True when an outdated contract's Effective Date (a date, or None if
    blank/unreadable) is before its successor became mandatory -- it was
    legitimately written on the old form."""
    if effective is None:
        return False
    mm, dd, yyyy = MANDATORY_SINCE[current_for(form_id)].split("/")
    return effective < date(int(yyyy), int(mm), int(dd))


def outdated_contract_message(form_id: str, edition=None, effective=None) -> str:
    """txo-trec-qa's wording (review of bd80632, 2026-10-25) for an outdated
    main contract. Signed before the successor's mandatory date: info
    wording. Everything else, incl. a blank or unreadable Effective Date:
    the must-fix wording."""
    cur = current_for(form_id)
    title, cur_edition, _ = CURRENT_FORMS[cur]
    if signed_before_mandatory(form_id, effective):
        return (f"This contract is on TREC {form_id}, and its Effective Date ({effective:%B %-d, %Y}) is before "
                f"TREC {cur} became mandatory on {MANDATORY_SINCE[cur]}. TC Check doesn't check replaced editions, "
                f"so nothing else was checked.")
    stamp = f" ({edition})" if edition else ""
    return (f"This file is on TREC {form_id}{stamp}. TREC {cur} ({cur_edition}) is the current {title}, "
            f"mandatory since {MANDATORY_SINCE[cur]}. TC Check doesn't check replaced editions, so nothing else "
            f"was checked. If this contract isn't signed yet, write it on TREC {cur}.")
