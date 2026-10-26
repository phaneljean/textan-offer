"""Builds filled Colorado CBS4 (Land) test files from the blank 2026 form,
by WIDGET INDEX: names are shared ("Solar Power Will Checkbox" is both
§2.5.5 Will and §2.7.1 Deeded Water Rights), so filling by name would check
both. Indexes are page-order widget positions in CBS4_blank_2026.pdf, from
txo-trec-qa's rect-verified cbs4_widgets.json (spec_co_land.md §5).

CLEAN is a two-party vacant-land deal at 123 Main St, Kiowa (Elbert County):
400,000 = 10,000 earnest + 300,000 Conventional + 90,000 cash, well with a
permit, items 28/29 dated, New Survey ordered and paid by Seller, closing
Fri 10/30/2026 with §3.3.3 Will Not. Items 11/13 fall on Cabrini Day (10/05)
and 30/31 on 10/12 -- not extended, by choice, so no finding.

    python3 tests/co_land_fixtures.py out.pdf   # writes the clean file
"""
import os
import sys

import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
BLANK = os.path.join(HERE, "fixtures", "CBS4_blank_2026.pdf")

LEGAL = ("A tract of land in the NE1/4 of Section 12, Township 9 South, Range 63 West of the 6th P.M., "
         "County of Elbert, State of Colorado, described by metes and bounds as follows: Beginning at the "
         "NE corner of said Section 12; thence S 89 deg 12' W 1320.00 ft; thence S 00 deg 15' E 660.00 ft; "
         "thence N 89 deg 12' E 1320.00 ft; thence N 00 deg 15' W 660.00 ft to the Point of Beginning.")

CLEAN = {
    0: True,                       # p1 Property with No Residences
    2: "10/01/2026",               # Date
    3: "John Buyer and Mary Buyer", 4: True,   # §2.1 Buyer, Joint Tenants
    8: "Robert Seller and Linda Seller",       # §2.3 Seller
    9: "Elbert", 10: LEGAL,
    11: "123 Main St", 12: "Kiowa", 13: "CO", 14: "80117",
    31: True, 32: "123456",        # §2.7.3 Well Rights + permit
    # §3.1 items 1-47 are widgets 37-83 (item n = widget 36 + n)
    37: "N/A", 38: "N/A",
    39: "10/08/2026", 40: "10/15/2026", 41: "10/08/2026", 42: "10/15/2026", 43: "10/20/2026", 44: "N/A",
    45: "N/A", 46: "N/A", 47: "10/05/2026", 48: "N/A",
    49: "10/05/2026", 50: "10/26/2026", 51: "10/26/2026", 52: "N/A", 53: "N/A", 54: "N/A", 55: "N/A", 56: "N/A",
    57: "N/A", 58: "10/20/2026", 59: "10/22/2026", 60: "10/23/2026",
    61: "10/14/2026", 62: "10/19/2026", 63: "10/22/2026",
    64: "10/19/2026", 65: "10/19/2026",
    66: "10/12/2026", 67: "10/12/2026", 68: "10/16/2026", 69: "10/16/2026",
    70: "10/08/2026", 71: "10/15/2026", 72: "10/20/2026", 73: "10/16/2026", 74: "N/A", 75: "N/A",
    76: "N/A", 77: "N/A", 78: "N/A",
    79: "10/30/2026", 80: "10/30/2026", 81: "5:00 PM", 82: "10/02/2026", 83: "5:00 PM",
    85: True,                      # §3.3.3 Will Not
    86: "400,000.00", 87: "10,000.00", 88: "300,000.00", 92: "90,000.00", 93: "400,000.00", 94: "400,000.00",
    96: "wire", 97: "Example Title Co",
    98: True, 100: True,           # §4.4.3 Does; §4.5.3 Conventional
    124: True, 126: True, 129: True, 131: True,   # appraisal Buyer; Seller selects title; OEC Will; OEC Buyer
    137: True,                     # tax certificate Seller
    140: True, 141: "ALTA/NSPS Land Title Survey", 142: True, 144: True, 147: "Lender",   # §9.1 New Survey
    175: True, 178: True,          # §10.8 Does / Does
    180: True, 182: "Example Title Co", 183: True,   # closing instructions Are; special warranty deed
    192: True, 197: True, 202: True, 206: True, 210: True, 214: True, 218: True, 229: True, 233: True,
    236: True,
    248: True, 249: "2.8",         # §29.1
    261: "John Buyer", 269: "Mary Buyer", 277: "Robert Seller", 285: "Linda Seller",
    268: "john@example.com", 284: "robert@example.com",
    # 295 (Buyer's Agent) left off so the HB26-1426 buyer-agreement review doesn't fire.
    293: True, 309: True, 311: True, 314: True,
}


def fill(overrides=None, out=None, src=BLANK):
    """overrides: {widget index: value}; None removes a CLEAN entry. Use " "
    for a blank text (PyMuPDF ignores "") and False for an unchecked box."""
    vals = dict(CLEAN)
    for k, v in (overrides or {}).items():
        if v is None:
            vals.pop(k, None)
        else:
            vals[k] = v
    doc = fitz.open(src)
    i = -1
    for page in doc:
        for w in page.widgets():
            i += 1
            if i not in vals:
                continue
            v = vals[i]
            if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                w.field_value = w.on_state() if v else "Off"
            else:
                w.field_value = v if v != "" else " "
            w.update()
    doc.save(out)
    doc.close()
    return out


if __name__ == "__main__":
    print("wrote", fill(out=sys.argv[1] if len(sys.argv) > 1 else "co_land_clean.pdf"))
