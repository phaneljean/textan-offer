"""Builds filled Colorado CBS1 test files from the blank 2026 form.

CLEAN is a complete, internally consistent financed deal at 123 Main St.
Tests make single-error variants of it with fill(overrides).

    python3 tests/co_fixtures.py   # writes co_cbs1_clean.pdf + renders p1/p3/p4/p5 PNGs for a visual check
"""
import os
import sys
import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
BLANK = os.path.join(HERE, "fixtures", "CBS1_blank_2026.pdf")

CLEAN = {
    "Date": "10/01/2026",
    "Buyer Name": "Pat Buyer",
    "Seller Name": "Sam Seller",
    "County": "Denver",
    "Property Legal Description": "Lot 1, Block 2, Example Subdivision",
    "Street Address": "123 Main St",
    "City": "Denver",
    "State": "CO",
    "Zip Code": "80202",
    "Purchase Price": "500,000.00",
    "Earnest Money": "10,000.00",
    "New Loan": "400,000.00",
    "Cash At Closing": "90,000.00",
    "Total 01": "500,000.00",
    "Total 02": "500,000.00",
    "Earnest Money Form": "wire",
    "Earnest Money Holder": "Example Title Co",
    "Record Title Deadline and Tax Certificate": "10/08/2026",
    "Record Title Objection Deadline": "10/15/2026",
    "Off Record Title Deadline": "10/08/2026",
    "Off Record Title Objection Deadline": "10/15/2026",
    "Title Resolution Deadline": "10/20/2026",
    "Sellers Property Disclosure Deadline": "10/05/2026",
    "New Loan Application Deadline": "10/05/2026",
    "New Loan Terms Deadline": "10/26/2026",
    "New Loan Availability Deadline": "10/26/2026",
    "Appraisal Deadline": "10/20/2026",
    "Appraisal Objection Deadline": "10/22/2026",
    "Appraisal Resolution Deadline": "10/24/2026",
    "Inspection Termination Deadline": "10/12/2026",
    "Inspection Objection Deadline": "10/12/2026",
    "Inspection Resolution Deadline": "10/16/2026",
    "Closing Date": "10/30/2026",          # a Friday
    "Possession Date": "10/30/2026",
    "Possession Time": "5:00 PM",
    "Acceptance Deadline Date": "10/02/2026",
    "Acceptance Deadline Time": "5:00 PM",
    "Conventional Checkbox": True,
    "Available Funds Does Checkbox": True,
    "Seller Selects Title Checkbox": True,
    "Deadlines Will Not Checkbox": True,
    "Closing Services Fee 50 50 Checkbox": True,
}


def fill(overrides=None, out=None, base=None):
    vals = dict(CLEAN if base is None else base)
    for k, v in (overrides or {}).items():
        if v is None:
            vals.pop(k, None)
        else:
            vals[k] = v
    doc = fitz.open(BLANK)
    for page in doc:
        for w in page.widgets():
            # Widgets carry leaf names ("1 Checkbox"); readers see qualified ones ("29.1 Checkbox").
            name = next((k for k in (w.field_name, f"29.{w.field_name}") if k in vals), None)
            if name is None or (name != w.field_name and page.number != 17):
                continue
            v = vals[name]
            if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                w.field_value = w.on_state() if v else "Off"
            else:
                w.field_value = str(v)
            w.update()
    doc.save(out)
    doc.close()
    return out


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "co_cbs1_clean.pdf"
    fill(out=out)
    d = fitz.open(out)
    for p in (1, 3, 4, 5, 8):
        d[p - 1].get_pixmap(dpi=90).save(f"{out[:-4]}_p{p}.png")
    print("wrote", out)
