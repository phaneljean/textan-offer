"""
co_forms.py -- the Colorado forms that travel with a CBS1, checked against
it: the Colorado counterpart of the 39-11 / 40-11 / 41-3 cross-checks in
tc_audit.py. Forms (Colorado Real Estate Commission, downloaded 2026-10-12
from dre.colorado.gov):

- AE41  Agreement to Amend/Extend Contract (mandatory 8-24)
- CP40  Counterproposal (mandatory 8-24)
- NTC43 Inspection Objection Notice
- SWA35 Source of Water Addendum (mandatory 1-11)
- RA33  Residential Addendum (mandatory 8-24) -- recognized, not audited

AE41 and CP40 repeat the whole §3.1 table with three columns per row:
"Date or Deadline" (the new value), "No Change" and "Deleted". Their field
names are inconsistent and sometimes wrong, so cells are mapped by their
position next to the printed row label -- see AMEND_CELLS.

The useful check isn't "is the amendment filled in" but "is the contract
still coherent AFTER it": apply_changes() overlays the amendment on the
contract and co_audit.audit_values() runs again; only findings the
amendment itself introduced are reported (e.g. an extended Inspection
Resolution Deadline that now lands after the unchanged Closing Date).
"""
import re
import co_audit
from co_audit import _v, _parse_date, _money, _fmt_money, _issue, _blank_or_na

FORM_NAMES = {
    "ae41": "Agreement to Amend/Extend",
    "ctr": "Counterproposal",
    "insp": "Inspection Objection Notice",
    "swa": "Source of Water Addendum",
    "resadd": "Residential Addendum",
    "ertb": "Exclusive Right-to-Buy Listing Contract",
}

FINGERPRINTS = {
    "ae41": ["Additional Amendments Field", "Original Contract Date", "Other Dates or Deadlines Field"],
    "ctr": ["Not Part of Counterproposal", "Other Changes", "Original Contract Date"],
    "insp": ["Description of What Is Unsatisfactory", "Number of Additional Pages", "Property Name"],
    "swa": ["If a well is the source of water for the Property a copy of the current Well Permit", "Contract Date"],
    "resadd": ["1 Checkbox Land", "Original Contract Date 01", "Metro District Website"],
    "ertb": ["Listing Period Starts", "Listing Period Ends", "Buyer Agency", "Transaction Brokerage"],
}

# BC60 Exclusive Right-to-Buy (2026): field names are fully qualified by
# section ("7.1.1.1 Percentage", "7.3.2 Checkbox") and unique -- checked
# 2026-10-12 by rendering. §7 compensation fields:
ERTB_COMP_FIELDS = ["7.1.1.1 Percentage", "7.1.1.1 Amount", "7.1.1.1 Other Description", "7.1.2 Hourly Fee",
                    "7.1.3 Retainer Fee", "7.1.4 Other Compensation"]
CBS1_BUYER_RELATIONSHIP = {
    "agent": "Broker Working With Buyer Buyers Agent Checkbox",
    "tb": "Broker Working With Buyer Transaction Broker Checkbox",
    "customer": "Broker Working With Buyer Customer Checkbox",
}

PRICE_FIELDS = ["Purchase Price", "Earnest Money", "New Loan", "Assumption Balance", "Private Balance",
                "Seller Financing", "Cash At Closing", "Total 01", "Total 02"]
# CP40 calls Private Balance "Private Financing".
CTR_PRICE_ALIASES = {"Private Balance": "Private Financing"}

# Forms §31 might list that we can recognize when attached.
LISTED_FORMS = [("swa", re.compile(r"source\s+of\s+water|\bSWA\s*35", re.I)),
                ("resadd", re.compile(r"residential\s+addendum|\bRA\s*33", re.I))]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def classify(values: dict):
    if co_audit.is_cbs1(values):
        return "cbs1"
    for kind, names in FINGERPRINTS.items():
        if sum(1 for n in names if n in values) >= len(names) - (0 if len(names) <= 2 else 1):
            return kind
    return None


# Cell -> §3.1 row map for AE41 and CP40, built 2026-10-12 by matching
# each widget's position to the printed row label beside it (field names
# can't be trusted: on AE41 the Deleted cell of Loan Transfer, both New ILC
# rows and Lead-Based Paint Termination is ONE shared field, "Deleted Loan
# Transfer" -- checking one checks all four -- so those Deleted cells are
# ignored as ambiguous). Rows keyed by the CBS1's own field name.
AMEND_CELLS = {'ae41': {'Alternative Earnest Money Deadline': {'deleted': 'DeletedAlternative Earnest Money Deadline',
                                                 'new': 'Date or DeadlineAlternative Earnest Money Deadline',
                                                 'nochange': 'No ChangeAlternative Earnest Money Deadline'},
          'Appraisal Deadline': {'deleted': 'Appraisal Deadline Deleted',
                                 'new': 'Appraisal Deadline',
                                 'nochange': 'Appraisal Deadline No Change'},
          'Appraisal Objection Deadline': {'deleted': 'Appraisal Objection Deadline Deleted',
                                           'new': 'Appraisal Objection Deadline',
                                           'nochange': 'Appraisal Objection Deadline No Change'},
          'Appraisal Resolution Deadline': {'deleted': 'Appraisal Resolution Deadline Deleted',
                                            'new': 'Appraisal Resolution Deadline',
                                            'nochange': 'Appraisal Resolution Deadline No Change'},
          'Association Documents Deadline': {'deleted': 'DeletedAssociation Documents Deadline',
                                             'new': 'Date or DeadlineAssociation Documents Deadline',
                                             'nochange': 'No ChangeAssociation Documents Deadline'},
          'Association Documents Termination Deadline': {'deleted': 'DeletedAssociation Documents Termination '
                                                                    'Deadline',
                                                         'new': 'Date or DeadlineAssociation Documents Termination '
                                                                'Deadline',
                                                         'nochange': 'No ChangeAssociation Documents Termination '
                                                                     'Deadline'},
          'Buyers Credit Information Deadline': {'deleted': 'DeletedBuyers Credit Information Deadline',
                                                 'new': 'Date or DeadlineBuyers Credit Information Deadline',
                                                 'nochange': 'No ChangeBuyers Credit Information Deadline'},
          'Closing Date': {'deleted': 'Closing Date Deleted',
                           'new': 'Closing Date',
                           'nochange': 'Closing Date No Change'},
          'Conditional Sale Deadline': {'deleted': 'Conditional Sale Deadline Deleted',
                                        'new': 'Conditional Sale Deadline',
                                        'nochange': 'Conditional Sale Deadline No Change'},
          'Disapproval of Buyers Credit Information Deadline': {'deleted': 'DeletedDisapproval of Buyers Credit '
                                                                           'Information Deadline',
                                                                'new': 'Date or DeadlineDisapproval of Buyers Credit '
                                                                       'Information Deadline',
                                                                'nochange': 'No ChangeDisapproval of Buyers Credit '
                                                                            'Information Deadline'},
          'Due Diligence Documents Delivery Deadline': {'deleted': 'Due Diligence Documents Delivery Deadline '
                                                                   'Deleted',
                                                        'new': 'Due Diligence Documents Delivery Deadline',
                                                        'nochange': 'Due Diligence Documents Delivery Deadline No '
                                                                    'Change'},
          'Due Diligence Documents Objection Deadline': {'deleted': 'Due Diligence Documents Objection Deadline '
                                                                    'Deleted',
                                                         'new': 'Due Diligence Documents Objection Deadline',
                                                         'nochange': 'Due Diligence Documents Objection Deadline No '
                                                                     'Change'},
          'Due Diligence Documents Resolution Deadline': {'deleted': 'Due Diligence Documents Resolution Deadline '
                                                                     'Deleted',
                                                          'new': 'Due Diligence Documents Resolution Deadline',
                                                          'nochange': 'Due Diligence Documents Resolution Deadline '
                                                                      'No Change'},
          'Existing Loan Deadline': {'deleted': 'DeletedExisting Loan Deadline',
                                     'new': 'Date or DeadlineExisting Loan Deadline',
                                     'nochange': 'No ChangeExisting Loan Deadline'},
          'Existing Loan Termination Deadline': {'deleted': 'DeletedExisting Loan Termination Deadline',
                                                 'new': 'Date or DeadlineExisting Loan Termination Deadline',
                                                 'nochange': 'No ChangeExisting Loan Termination Deadline'},
          'Inspection Objection Deadline': {'deleted': 'Inspection Objection Deadline Deleted',
                                            'new': 'Inspection Objection Deadline',
                                            'nochange': 'Inspection Objection Deadline No Change'},
          'Inspection Resolution Deadline': {'deleted': 'Inspection Resolution Deadline Deleted',
                                             'new': 'Inspection Resolution Deadline',
                                             'nochange': 'Inspection Resolution Deadline No Change'},
          'Inspection Termination Deadline': {'deleted': 'Inspection Termination Deadline Deleted',
                                              'new': 'Inspection Termination Deadline',
                                              'nochange': 'Inspection Termination Deadline No Change'},
          'Lead Based Paint Disclosure Deadline': {'deleted': 'DeletedLeadBased Paint Disclosure Deadline',
                                                   'new': 'Date or DeadlineLeadBased Paint Disclosure Deadline',
                                                   'nochange': 'No ChangeLeadBased Paint Disclosure Deadline'},
          'LeadBased Paint Termination Deadline': {'deleted': 'Deleted Loan Transfer',
                                                   'new': 'LeadBased Paint Termination Deadline',
                                                   'nochange': 'LeadBased Paint Termination Deadline No Change'},
          'Loan Transfer Approval Deadline': {'deleted': 'Deleted Loan Transfer',
                                              'new': 'Loan Transfer Approval Deadline',
                                              'nochange': 'No Change Loan Transfer'},
          'Mineral Rights Examination Deadline': {'deleted': 'Mineral Rights Examination Deadline Deleted',
                                                  'new': 'Mineral Rights Examination Deadline',
                                                  'nochange': 'Mineral Rights Examination Deadline No Change'},
          'New ILC or New Survey Deadline': {'deleted': 'Deleted Loan Transfer',
                                             'new': 'New ILC or New Survey Deadline',
                                             'nochange': 'New ILC or New Survey Deadline No Change'},
          'New ILC or New Survey Objection Deadline': {'deleted': 'Deleted Loan Transfer',
                                                       'new': 'New ILC or New Survey Objection Deadline',
                                                       'nochange': 'New ILC or New Survey Objection Deadline No '
                                                                   'Change'},
          'New ILC or New Survey Resolution Deadline': {'deleted': 'New ILC or New Survey Resolution Deadline '
                                                                   'Deleted',
                                                        'new': 'New ILC or New Survey Resolution Deadline',
                                                        'nochange': 'New ILC or New Survey Resolution Deadline No '
                                                                    'Change'},
          'New Loan Application Deadline': {'deleted': 'DeletedNew Loan Application Deadline',
                                            'new': 'Date or DeadlineNew Loan Application Deadline',
                                            'nochange': 'No ChangeNew Loan Application Deadline'},
          'New Loan Availability Deadline': {'deleted': 'DeletedNew Loan Availability Deadline',
                                             'new': 'Date or DeadlineNew Loan Availability Deadline',
                                             'nochange': 'No ChangeNew Loan Availability Deadline'},
          'New Loan Terms Deadline': {'deleted': 'DeletedNew Loan Terms Deadline',
                                      'new': 'Date or DeadlineNew Loan Terms Deadline',
                                      'nochange': 'No ChangeNew Loan Terms Deadline'},
          'Off Record Title Deadline': {'deleted': 'DeletedOffRecord Title Deadline',
                                        'new': 'Date or DeadlineOffRecord Title Deadline',
                                        'nochange': 'No ChangeOffRecord Title Deadline'},
          'Off Record Title Objection Deadline': {'deleted': 'DeletedOffRecord Title Objection Deadline',
                                                  'new': 'Date or DeadlineOffRecord Title Objection Deadline',
                                                  'nochange': 'No ChangeOffRecord Title Objection Deadline'},
          'Possession Date': {'deleted': 'Possession Date Deleted',
                              'new': 'Possession Date',
                              'nochange': 'Possession Date No Change'},
          'Possession Time': {'deleted': 'Possession Time Deleted',
                              'new': 'Possession Time',
                              'nochange': 'Possession Time No Change'},
          'Property Insurance Termination Deadline': {'deleted': 'Property Insurance Termination Deadline Deleted',
                                                      'new': 'Property Insurance Termination Deadline',
                                                      'nochange': 'Property Insurance Termination Deadline No '
                                                                  'Change'},
          'Record Title Deadline and Tax Certificate': {'deleted': 'DeletedRecord Title Deadline and Tax Certificate',
                                                        'new': 'Date or DeadlineRecord Title Deadline and Tax '
                                                               'Certificate',
                                                        'nochange': 'No ChangeRecord Title Deadline and Tax '
                                                                    'Certificate'},
          'Record Title Objection Deadline': {'deleted': 'DeletedRecord Title Objection Deadline',
                                              'new': 'Date or DeadlineRecord Title Objection Deadline',
                                              'nochange': 'No ChangeRecord Title Objection Deadline'},
          'Seller or Private Financing Deadline': {'deleted': 'Seller or Private Financing Deadline Deleted',
                                                   'new': 'Seller or Private Financing Deadline',
                                                   'nochange': 'Seller or Private Financing Deadline No Change'},
          'Sellers Property Disclosure Deadline': {'deleted': 'DeletedSellers Property Disclosure Deadline',
                                                   'new': 'Date or DeadlineSellers Property Disclosure Deadline',
                                                   'nochange': 'No ChangeSellers Property Disclosure Deadline'},
          'Third Party Right to PurchaseApprove Deadline': {'deleted': 'DeletedThird Party Right to PurchaseApprove '
                                                                       'Deadline',
                                                            'new': 'Date or DeadlineThird Party Right to '
                                                                   'PurchaseApprove Deadline',
                                                            'nochange': 'No ChangeThird Party Right to '
                                                                        'PurchaseApprove Deadline'},
          'Time of Day Deadline': {'deleted': 'DeletedTime of Day Deadline',
                                   'new': 'Date or DeadlineTime of Day Deadline',
                                   'nochange': 'No ChangeTime of Day Deadline'},
          'Title Resolution Deadline': {'deleted': 'DeletedTitle Resolution Deadline',
                                        'new': 'Date or DeadlineTitle Resolution Deadline',
                                        'nochange': 'No ChangeTitle Resolution Deadline'},
          'Water Rights Examination Deadline': {'deleted': 'Water Rights Examination Deadline Deleted',
                                                'new': 'Water Rights Examination Deadline',
                                                'nochange': 'Water Rights Examination Deadline No Change'}},
 'ctr': {'Alternative Earnest Money Deadline': {'deleted': 'DeletedAlternative Earnest Money Deadline',
                                                'new': 'Date or DeadlineAlternative Earnest Money Deadline',
                                                'nochange': 'No ChangeAlternative Earnest Money Deadline'},
         'Appraisal Deadline': {'deleted': 'DeletedAppraisal Deadline',
                                'new': 'Date or DeadlineAppraisal Deadline',
                                'nochange': 'No ChangeAppraisal Deadline'},
         'Appraisal Objection Deadline': {'deleted': 'DeletedAppraisal Objection Deadline',
                                          'new': 'Date or DeadlineAppraisal Objection Deadline',
                                          'nochange': 'No ChangeAppraisal Objection Deadline'},
         'Appraisal Resolution Deadline': {'deleted': 'Appraisal Resolution Deleted',
                                           'new': 'Appraisal Resolution Deadline',
                                           'nochange': 'Appraisal Resolution Deadline No Change'},
         'Association Documents Deadline': {'deleted': 'DeletedAssociation Documents Deadline',
                                            'new': 'Date or DeadlineAssociation Documents Deadline',
                                            'nochange': 'No ChangeAssociation Documents Deadline'},
         'Association Documents Termination Deadline': {'deleted': 'DeletedAssociation Documents Termination '
                                                                   'Deadline',
                                                        'new': 'Date or DeadlineAssociation Documents Termination '
                                                               'Deadline',
                                                        'nochange': 'No ChangeAssociation Documents Termination '
                                                                    'Deadline'},
         'Buyers Credit Information Deadline': {'deleted': 'DeletedBuyers Credit Information Deadline',
                                                'new': 'Date or DeadlineBuyers Credit Information Deadline',
                                                'nochange': 'No ChangeBuyers Credit Information Deadline'},
         'Closing Date': {'deleted': 'Closing Date Deleted',
                          'new': 'Closing Date',
                          'nochange': 'Closing Date No Change'},
         'Conditional Sale Deadline': {'deleted': 'Conditional Sale Deadline Deleted',
                                       'new': 'Conditional Sale Deadline',
                                       'nochange': 'Conditional Sale Deadline No Change'},
         'Disapproval of Buyers Credit Information Deadline': {'deleted': 'DeletedDisapproval of Buyers Credit '
                                                                          'Information Deadline',
                                                               'new': 'Date or DeadlineDisapproval of Buyers Credit '
                                                                      'Information Deadline',
                                                               'nochange': 'No ChangeDisapproval of Buyers Credit '
                                                                           'Information Deadline'},
         'Due Diligence Documents Delivery Deadline': {'deleted': 'Due Diligence Documents Delivery Deadline Deleted',
                                                       'new': 'Due Diligence Documents Delivery Deadline',
                                                       'nochange': 'Due Diligence Documents Delivery Deadline No '
                                                                   'Change'},
         'Due Diligence Documents Objection Deadline': {'deleted': 'Due Diligence Documents Objection Deadline '
                                                                   'Deleted',
                                                        'new': 'Due Diligence Documents Objection Deadline',
                                                        'nochange': 'Due Diligence Documents Objection Deadline No '
                                                                    'Change'},
         'Due Diligence Documents Resolution Deadline': {'deleted': 'Due Diligence Documents Resolution Deadline '
                                                                    'Deleted',
                                                         'new': 'Due Diligence Documents Resolution Deadline',
                                                         'nochange': 'Due Diligence Documents Resolution Deadline No '
                                                                     'Change'},
         'Existing Loan Deadline': {'deleted': 'DeletedExisting Loan Deadline',
                                    'new': 'Date or DeadlineExisting Loan Deadline',
                                    'nochange': 'No ChangeExisting Loan Deadline'},
         'Existing Loan Termination Deadline': {'deleted': 'DeletedExisting Loan Termination Deadline',
                                                'new': 'Date or DeadlineExisting Loan Termination Deadline',
                                                'nochange': 'No ChangeExisting Loan Termination Deadline'},
         'Inspection Objection Deadline': {'deleted': 'Inspection Objection Deadline Deleted',
                                           'new': 'Inspection Objection Deadline',
                                           'nochange': 'Inspection Objection Deadline No Change'},
         'Inspection Resolution Deadline': {'deleted': 'Inspection Resolution Deadline Deleted',
                                            'new': 'Inspection Resolution Deadline',
                                            'nochange': 'Inspection Resolution Deadline No Change'},
         'Inspection Termination Deadline': {'deleted': 'Inspection Termination Deadline Deleted',
                                             'new': 'Inspection Termination Deadline',
                                             'nochange': 'Inspection Termination Deadline No Change'},
         'Lead Based Paint Disclosure Deadline': {'deleted': 'DeletedLeadBased Paint Disclosure Deadline',
                                                  'new': 'Date or DeadlineLeadBased Paint Disclosure Deadline',
                                                  'nochange': 'No ChangeLeadBased Paint Disclosure Deadline'},
         'LeadBased Paint Termination Deadline': {'deleted': 'LeadBased Paint Termination Deadline Deleted',
                                                  'new': 'LeadBased Paint Termination Deadline',
                                                  'nochange': 'LeadBased Paint Termination Deadline No Change'},
         'Loan Transfer Approval Deadline': {'deleted': 'DeletedLoan Transfer Approval Deadline',
                                             'new': 'Date or DeadlineLoan Transfer Approval Deadline',
                                             'nochange': 'No ChangeLoan Transfer Approval Deadline'},
         'Mineral Rights Examination Deadline': {'deleted': 'Mineral Rights Examination Deadline Deleted',
                                                 'new': 'Mineral Rights Examination Deadline',
                                                 'nochange': 'Mineral Rights Examination Deadline No Change'},
         'New ILC or New Survey Deadline': {'deleted': 'New ILC or New Survey Deadline Deleted',
                                            'new': 'New ILC or New Survey Deadline',
                                            'nochange': 'New ILC or New Survey Deadline No Change'},
         'New ILC or New Survey Objection Deadline': {'deleted': 'New ILC or New Survey Objection Deadline Deleted',
                                                      'new': 'New ILC or New Survey Objection Deadline',
                                                      'nochange': 'New ILC or New Survey Objection Deadline No '
                                                                  'Change'},
         'New ILC or New Survey Resolution Deadline': {'deleted': 'New ILC or New Survey Resolution Deadline Deleted',
                                                       'new': 'New ILC or New Survey Resolution Deadline',
                                                       'nochange': 'New ILC or New Survey Resolution Deadline No '
                                                                   'Change'},
         'New Loan Application Deadline': {'deleted': 'DeletedNew Loan Application Deadline',
                                           'new': 'Date or DeadlineNew Loan Application Deadline',
                                           'nochange': 'No ChangeNew Loan Application Deadline'},
         'New Loan Availability Deadline': {'deleted': 'DeletedNew Loan Availability Deadline',
                                            'new': 'Date or DeadlineNew Loan Availability Deadline',
                                            'nochange': 'No ChangeNew Loan Availability Deadline'},
         'New Loan Terms Deadline': {'deleted': 'DeletedNew Loan Terms Deadline',
                                     'new': 'Date or DeadlineNew Loan Terms Deadline',
                                     'nochange': 'No ChangeNew Loan Terms Deadline'},
         'Off Record Title Deadline': {'deleted': 'DeletedOffRecord Title Deadline',
                                       'new': 'Date or DeadlineOffRecord Title Deadline',
                                       'nochange': 'No ChangeOffRecord Title Deadline'},
         'Off Record Title Objection Deadline': {'deleted': 'DeletedOffRecord Title Objection Deadline',
                                                 'new': 'Date or DeadlineOffRecord Title Objection Deadline',
                                                 'nochange': 'No ChangeOffRecord Title Objection Deadline'},
         'Possession Date': {'deleted': 'Possession Date Deleted',
                             'new': 'Possession Date',
                             'nochange': 'Possession Date No Change'},
         'Possession Time': {'deleted': 'Possession Time Deleted',
                             'new': 'Possession Time',
                             'nochange': 'Possession Time No Change'},
         'Property Insurance Termination Deadline': {'deleted': 'Property Insurance Termination Deadline Deleted',
                                                     'new': 'Property Insurance Termination Deadline',
                                                     'nochange': 'Property Insurance Termination Deadline No Change'},
         'Record Title Deadline and Tax Certificate': {'deleted': 'DeletedRecord Title Deadline and Tax Certificate',
                                                       'new': 'Date or DeadlineRecord Title Deadline and Tax '
                                                              'Certificate',
                                                       'nochange': 'No ChangeRecord Title Deadline and Tax '
                                                                   'Certificate'},
         'Record Title Objection Deadline': {'deleted': 'DeletedRecord Title Objection Deadline',
                                             'new': 'Date or DeadlineRecord Title Objection Deadline',
                                             'nochange': 'No ChangeRecord Title Objection Deadline'},
         'Seller or Private Financing Deadline': {'deleted': 'DeletedSeller or Private Financing Deadline',
                                                  'new': 'Date or DeadlineSeller or Private Financing Deadline',
                                                  'nochange': 'No ChangeSeller or Private Financing Deadline'},
         'Sellers Property Disclosure Deadline': {'deleted': 'DeletedSellers Property Disclosure Deadline',
                                                  'new': 'Date or DeadlineSellers Property Disclosure Deadline',
                                                  'nochange': 'No ChangeSellers Property Disclosure Deadline'},
         'Third Party Right to PurchaseApprove Deadline': {'deleted': 'DeletedThird Party Right to PurchaseApprove '
                                                                      'Deadline',
                                                           'new': 'Date or DeadlineThird Party Right to '
                                                                  'PurchaseApprove Deadline',
                                                           'nochange': 'No ChangeThird Party Right to '
                                                                       'PurchaseApprove Deadline'},
         'Time of Day Deadline': {'deleted': 'DeletedTime of Day Deadline',
                                  'new': 'Date or DeadlineTime of Day Deadline',
                                  'nochange': 'No ChangeTime of Day Deadline'},
         'Title Resolution Deadline': {'deleted': 'DeletedTitle Resolution Deadline',
                                       'new': 'Date or DeadlineTitle Resolution Deadline',
                                       'nochange': 'No ChangeTitle Resolution Deadline'},
         'Water Rights Examination Deadline': {'deleted': 'Water Rights Examination Deadline Deleted',
                                               'new': 'Water Rights Examination Deadline',
                                               'nochange': 'Water Rights Examination Deadline No Change'}}}
AMBIGUOUS_CELLS = {"Deleted Loan Transfer"}


def amendment_changes(values: dict, kind: str):
    """({cbs1 field: new raw value, or None when the row is Deleted},
    [rows with more than one column filled]). Rows marked No Change or left
    untouched aren't in the dict."""
    changes, conflicts = {}, []
    for row, cells in AMEND_CELLS[kind].items():
        filled = {}
        for col, name in cells.items():
            if name in AMBIGUOUS_CELLS:
                continue
            raw = (values.get(name) or "").strip()
            if raw and raw.lower() not in ("off", "/off"):
                filled[col] = raw
        if len(filled) > 1:
            conflicts.append(row)
        elif "deleted" in filled:
            changes[row] = None
        elif "new" in filled:
            # "N/A" written as the new value deletes the row too (§3.2).
            changes[row] = None if _blank_or_na(filled["new"]) else filled["new"]
    return changes, conflicts


def apply_changes(contract: dict, changes: dict, price_table: dict = None) -> dict:
    merged = dict(contract)
    for field, new in changes.items():
        merged[field] = new or "Deleted"   # §3.2 treats "Deleted" like blank
    if price_table:
        for f in PRICE_FIELDS:
            merged[f] = price_table.get(f, "")
    return merged


def _ctr_price_table(values: dict):
    table = {f: _v(values, CTR_PRICE_ALIASES.get(f, f)) for f in PRICE_FIELDS}
    return table if any(table.values()) else None


_SUFFIX = re.compile(r"\b(st|street|ave|avenue|dr|drive|rd|road|ln|lane|ct|court|blvd|boulevard)\b\.?")


def _same_street(a: str, b: str) -> bool:
    na, nb = _norm(_SUFFIX.sub("", a.lower())), _norm(_SUFFIX.sub("", b.lower()))
    return bool(na and nb) and (na in nb or nb in na)


def cross_check(contract: dict, others: list) -> list:
    """others: [(kind, values)] for every non-CBS1 file classify() knew.
    Returns issues in co_audit's format."""
    issues = []
    street = _v(contract, co_audit.F["street"])
    contract_date = _parse_date(_v(contract, co_audit.F["date"]))
    attached = {k for k, _ in others}

    effective = dict(contract)          # contract as amended, in upload order
    base_keys = {i["key"] for i in co_audit.audit_values(contract)}

    for kind, values in others:
        label = FORM_NAMES[kind]
        # Belongs to this contract?
        other_street = _v(values, "Property Name") if kind == "insp" else _v(values, "Street Address")
        if street and other_street and not _same_street(street, other_street):
            issues.append(_issue("warning", f"{label}: property ({other_street}) doesn't match the contract's ({street}) -- "
                                            f"confirm it belongs to this file", f"co_{kind}_address_mismatch"))
        ocd = _parse_date(_v(values, "Original Contract Date") or _v(values, "Contract Date") or _v(values, "Original Contract Date 01"))
        if contract_date and ocd and ocd != contract_date:
            issues.append(_issue("warning", f"{label}: contract date {ocd:%m/%d/%Y} doesn't match the CBS1's date "
                                            f"({contract_date:%m/%d/%Y})", f"co_{kind}_contract_date_mismatch"))

        if kind in ("ae41", "ctr"):
            changes, conflicts = amendment_changes(values, kind)
            for field in conflicts:
                issues.append(_issue("blocker", f"{label}: {field} has more than one of new date / No Change / Deleted "
                                                f"filled in", "co_amend_row_conflict"))
            price_table = _ctr_price_table(values) if kind == "ctr" else None
            if price_table:
                old, new = _money(_v(contract, "Purchase Price")), _money(price_table["Purchase Price"])
                if old is not None and new is not None and abs(old - new) > 0.5:
                    issues.append(_issue("warning", f"{label} changes the Purchase Price from {_fmt_money(old)} to "
                                                    f"{_fmt_money(new)} -- confirm the loan and Cash at Closing in its "
                                                    f"§4 table match", "co_amend_price_change"))
            if not changes and not conflicts and not price_table and not _v(values, "Additional Amendments Field") \
                    and not _v(values, "Other Changes"):
                issues.append(_issue("warning", f"{label}: no dates, price or other changes are filled in", f"co_{kind}_empty"))
            effective = apply_changes(effective, changes, price_table)

    # Re-audit the contract as amended; report only what the amendments added.
    if attached & {"ae41", "ctr"}:
        for i in co_audit.audit_values(effective):
            if i["key"] not in base_keys:
                issues.append(_issue(i["severity"], f"After the amendment: {i['message']}", "co_amend_" + i["key"]))

    for kind, values in others:
        if kind == "insp":
            issues += _check_inspection_notice(effective, values)
        if kind == "ertb":
            issues += _check_buyer_listing(contract, values)

    # HB26-1426 (effective Aug 12, 2026, C.R.S. 12-10-403): a buyer's agent or
    # transaction-broker needs a signed written agreement with the buyer
    # before performing licensed duties. Only raised when the CBS1 itself says
    # the buyer's broker is acting as one of those (not "Customer").
    rel = [k for k, f in CBS1_BUYER_RELATIONSHIP.items() if co_audit._is_checked(contract, f)]
    if set(rel) & {"agent", "tb"} and "ertb" not in attached:
        issues.append(_issue("warning", "The buyer's broker is marked as Buyer's Agent or Transaction-Broker. Since Aug 12, "
                                        "2026 (HB26-1426) that needs a signed written agreement with the buyer -- attach the "
                                        "Exclusive Right-to-Buy so it's checked too", "co_ertb_not_attached"))

    # §31.1 lists a form we can recognize, but it isn't attached.
    listed = _v(contract, "Documents Part of the Contract")
    for kind, pattern in LISTED_FORMS:
        if listed and pattern.search(listed) and kind not in attached:
            issues.append(_issue("warning", f"§31.1 lists the {FORM_NAMES[kind]}, but it isn't attached here -- "
                                            f"attach it so it's checked too", f"co_{kind}_listed_missing"))
    return issues


def _check_buyer_listing(contract: dict, values: dict) -> list:
    """BC60 Exclusive Right-to-Buy against the CBS1 it should cover."""
    issues = []
    label = FORM_NAMES["ertb"]
    buyer = _v(values, "Buyer")
    cbs1_buyer = _v(contract, co_audit.F["buyer"])
    if not buyer:
        issues.append(_issue("blocker", f"{label}: Buyer (§3.1) is blank", "co_ertb_buyer_blank"))
    elif cbs1_buyer:
        a = {w for w in re.findall(r"[a-z]{2,}", buyer.lower())}
        b = {w for w in re.findall(r"[a-z]{2,}", cbs1_buyer.lower())}
        if a and b and not (a & b):
            issues.append(_issue("warning", f"{label}: Buyer ({buyer}) doesn't match the contract's Buyer ({cbs1_buyer})",
                                 "co_ertb_buyer_mismatch"))
    agency, tb = co_audit._is_checked(values, "Buyer Agency"), co_audit._is_checked(values, "Transaction Brokerage")
    if agency and tb:
        issues.append(_issue("blocker", f"{label}: both Buyer Agency and Transaction-Brokerage are checked", "co_ertb_relationship_conflict"))
    elif not (agency or tb):
        issues.append(_issue("warning", f"{label}: neither Buyer Agency nor Transaction-Brokerage is checked at the top",
                             "co_ertb_relationship_blank"))
    else:
        cbs1_agent = co_audit._is_checked(contract, CBS1_BUYER_RELATIONSHIP["agent"])
        cbs1_tb = co_audit._is_checked(contract, CBS1_BUYER_RELATIONSHIP["tb"])
        if (agency and cbs1_tb and not cbs1_agent) or (tb and cbs1_agent and not cbs1_tb):
            said = "Buyer Agency" if agency else "Transaction-Brokerage"
            cbs1_said = "Transaction-Broker" if cbs1_tb else "Buyer's Agent"
            issues.append(_issue("warning", f"{label} says {said}, but the CBS1 marks the buyer's broker as {cbs1_said}",
                                 "co_ertb_relationship_mismatch"))
    if not any(_v(values, f) for f in ERTB_COMP_FIELDS):
        issues.append(_issue("warning", f"{label}: no compensation is filled in under §7", "co_ertb_comp_blank"))
    start, end = _parse_date(_v(values, "Listing Period Starts")), _parse_date(_v(values, "Listing Period Ends"))
    signed = _parse_date(_v(contract, co_audit.F["date"]))
    if not (start and end):
        issues.append(_issue("blocker", f"{label}: the Listing Period start or end date is blank", "co_ertb_period_blank"))
    elif signed and not (start <= signed <= end):
        issues.append(_issue("blocker", f"{label}: the contract is dated {signed:%m/%d/%Y}, outside the Listing Period "
                                        f"({start:%m/%d/%Y} to {end:%m/%d/%Y})", "co_ertb_period_outside"))
    return issues


def _check_inspection_notice(contract: dict, values: dict) -> list:
    issues = []
    label = FORM_NAMES["insp"]
    if not _v(values, "Description of What Is Unsatisfactory"):
        issues.append(_issue("blocker", f"{label}: the description of what is unsatisfactory is blank -- §10.3.2 "
                                        f"requires a written description", "co_insp_description_blank"))
    notice = _parse_date(_v(values, "Date"))
    raw_deadline = _v(contract, co_audit.DEADLINES["insp_obj"][0])
    deadline = _parse_date(raw_deadline)
    if _blank_or_na(raw_deadline):
        issues.append(_issue("warning", f"{label} attached, but the contract's Inspection Objection Deadline is blank or "
                                        f"N/A -- under §3.2 that provision is deleted", "co_insp_no_deadline"))
    elif notice and deadline and notice > deadline:
        issues.append(_issue("blocker", f"{label} is dated {notice:%m/%d/%Y}, after the Inspection Objection Deadline "
                                        f"({deadline:%m/%d/%Y}). §10.3.2 requires delivery on or before that deadline -- "
                                        f"confirm when it was actually delivered", "co_insp_late"))
    return issues


# --- "What changed" between two versions of the same CBS1 ------------------
# (label, CBS1 field, is_money) -- price table first, then parties/property,
# then every §3.1 row in table order, the same order a TC scans a diff.
COMPARE_FIELDS = (
    [("Purchase Price", "Purchase Price", True), ("Earnest Money", "Earnest Money", True),
     ("New Loan", "New Loan", True), ("Assumption Balance", "Assumption Balance", True),
     ("Private Financing", "Private Balance", True), ("Seller Financing", "Seller Financing", True),
     ("Cash at Closing", "Cash At Closing", True), ("Seller Concession", "Seller Concessions", True),
     ("Earnest Money Holder", "Earnest Money Holder", False), ("Buyer", "Buyer Name", False),
     ("Seller", "Seller Name", False), ("Street Address", "Street Address", False),
     ("County", "County", False)]
    + [(label, name, False) for name, item, label in sorted(co_audit.DEADLINES.values(), key=lambda r: r[1])]
    + [("Possession Time", "Possession Time", False), ("Acceptance Deadline Time", "Acceptance Deadline Time", False),
       ("Additional Provisions (§30)", "Additional Provisions", False)]
)


def compare_cbs1(orig: dict, upd: dict) -> dict:
    """Same result shape as tc_audit.compare_contracts."""
    changes = []
    for label, name, is_money in COMPARE_FIELDS:
        o_raw, u_raw = _v(orig, name), _v(upd, name)
        if not o_raw and not u_raw:
            changes.append({"field": label, "status": "missing", "key": name})
            continue
        if is_money:
            same = _money(o_raw) == _money(u_raw)
        else:
            od, ud = _parse_date(o_raw), _parse_date(u_raw)
            same = (od == ud) if (od or ud) else re.sub(r"\s+", " ", o_raw.lower()) == re.sub(r"\s+", " ", u_raw.lower())
        if same:
            changes.append({"field": label, "status": "unchanged", "key": name})
        else:
            changes.append({"field": label, "status": "changed", "key": name,
                            "from": o_raw or "(blank)", "to": u_raw or "(blank)"})
    return {"recognized": True, "form": co_audit.FORM_LABEL, "changes": changes,
            "not_compared": [], "not_compared_reason": ""}
