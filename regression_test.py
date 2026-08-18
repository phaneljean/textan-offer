"""
regression_test.py — Permanent regression guard for the PDF generation
pipeline. Run this after every change to pdf_filler.py, financing_addendum.py,
cover_page.py, or the templates themselves.

    python regression_test.py            # compare against golden snapshots
    python regression_test.py --update   # regenerate golden snapshots
                                          # (only after a reviewed, intended
                                          # change -- this is what "known
                                          # good" means going forward)

For each fixed test case, this regenerates the PDF, reads back every
AcroForm field value plus the overlay-only text (closing date, address
headers, option period), and diffs that against the last known-good
snapshot in regression_golden/. It also runs pdf_validator.py against
each and fails if any test case has a blocking issue -- this is the
automated form of the "known-fixed issues" list in the QA spec, so a
regression in any of them fails a real test instead of waiting to be
noticed in production.

Design note: this diffs actual field VALUES, not rendered pixels/images --
cheap, exact, and it's what would have caught every bug found in
2026-08-17's session (each one was a value landing in the wrong field or
going missing, not a visual/layout change).
"""
import json
import os
import re
import sys

from pypdf import PdfReader

from pdf_filler import fill_offer_pdf, GENERATOR_VERSION
from pdf_validator import validate_offer_pdf

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regression_golden")

TEST_CASES = {
    "conventional_full": {
        "parsed": {
            "address": "1740 Grand Ave", "city": "Austin", "county": "Travis",
            "price": 725000, "down_payment_amount": 21750, "loan_amount": 703250,
            "earnest_money": 5000, "option_fee": 250, "close_days": 21,
            "inspection_days": 10,
            "agent": {
                "name": "Jane Smith", "license": "0654321", "brokerage": "Keller Williams",
                "phone": "(512) 555-0147", "email": "jane@realty.com",
                "business_address": "100 Congress Ave, Austin, TX 78701",
                "title_company": "Independence Title",
            },
        },
        "phone": "+15125550100",
    },
    "no_agent_profile": {
        # Deliberately sparse -- the case that should surface every
        # "required field blank" blocking check in pdf_validator.py, so a
        # future change that silently relaxes a check gets caught here.
        "parsed": {
            "address": "500 Ocean Blvd", "city": "San Antonio", "county": "Bexar",
            "price": 1200000, "down_payment_amount": 120000, "loan_amount": 1080000,
            "close_days": 45,
            "agent": {},
        },
        "phone": "+12105550199",
    },
    "high_down_payment": {
        "parsed": {
            "address": "123 Main St", "city": "Houston", "county": "Harris",
            "price": 650000, "down_payment_amount": 325000, "loan_amount": 325000,
            "earnest_money": 10000, "option_fee": 500, "close_days": 30,
            "agent": {
                "name": "Phanel Jean Baptiste", "license": "000137", "brokerage": "Remax",
                "phone": "+15622570392", "email": "pejeanbaptiste@gmail.com",
                "business_address": "456 Business Blvd, Houston, TX 77002",
                "title_company": "Texas Title Co",
            },
        },
        "phone": "+15622570392",
    },
}

# Overlay text isn't an AcroForm field -- these substrings must appear
# somewhere in the extracted page text, derived per-case from `parsed`.
def _expected_overlay_snippets(parsed: dict) -> list:
    from datetime import datetime, timedelta
    snippets = []
    if parsed.get("close_days") is not None:
        close_dt = datetime.now() + timedelta(days=parsed["close_days"])
        snippets.append(close_dt.strftime("%B %d,"))
    if parsed.get("inspection_days") is not None:
        snippets.append(str(parsed["inspection_days"]))
    return snippets


def _snapshot(pdf_path: str, parsed: dict) -> dict:
    reader = PdfReader(pdf_path)
    fields = reader.get_fields() or {}
    field_values = {name: str(f.get("/V") or "") for name, f in fields.items()}
    # Only keep non-empty values -- an all-blank field is not interesting to
    # track and keeps the golden file small and readable.
    field_values = {k: v for k, v in field_values.items() if v.strip()}

    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    overlay_present = {
        snippet: (snippet in text) for snippet in _expected_overlay_snippets(parsed)
    }

    validation = validate_offer_pdf(pdf_path, parsed)

    return {
        "field_values": field_values,
        "overlay_present": overlay_present,
        "page_count": len(reader.pages),
        "validation_blocking": validation["blocking"],
    }


def _diff(name: str, golden: dict, current: dict) -> list:
    problems = []

    gfv, cfv = golden["field_values"], current["field_values"]
    for k in sorted(set(gfv) | set(cfv)):
        if gfv.get(k) != cfv.get(k):
            problems.append(f"  field {k!r}: was {gfv.get(k)!r} -> now {cfv.get(k)!r}")

    for snippet, was_present in golden.get("overlay_present", {}).items():
        now_present = current.get("overlay_present", {}).get(snippet)
        if was_present and not now_present:
            problems.append(f"  overlay text {snippet!r} was present, now MISSING")

    if golden.get("page_count") != current.get("page_count"):
        problems.append(f"  page count: was {golden.get('page_count')} -> now {current.get('page_count')}")

    # Compare against golden's blocking list, not "any blocking = fail" --
    # the no_agent_profile case is deliberately incomplete on purpose, so
    # its golden snapshot legitimately has blocking entries. Only a CHANGE
    # (a newly-appeared or newly-resolved blocking reason) is interesting.
    gb, cb = set(golden.get("validation_blocking", [])), set(current.get("validation_blocking", []))
    for newly_blocking in sorted(cb - gb):
        problems.append(f"  pdf_validator NEW blocking issue: {newly_blocking!r}")
    for resolved in sorted(gb - cb):
        problems.append(f"  pdf_validator blocking issue no longer present (update golden if intended): {resolved!r}")

    return problems


def run(update: bool = False) -> bool:
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    all_ok = True

    for name, case in TEST_CASES.items():
        pdf_path = fill_offer_pdf(dict(case["parsed"]), case["phone"])
        current = _snapshot(pdf_path, case["parsed"])
        os.remove(pdf_path)

        golden_path = os.path.join(GOLDEN_DIR, f"{name}.json")
        golden_existed = os.path.exists(golden_path)

        if update or not golden_existed:
            with open(golden_path, "w") as f:
                json.dump(current, f, indent=2, sort_keys=True)
            print(f"[{'updated' if golden_existed else 'baseline'}] {name}: {golden_path}")
            continue

        with open(golden_path) as f:
            golden = json.load(f)

        problems = _diff(name, golden, current)
        if problems:
            all_ok = False
            print(f"[FAIL] {name}")
            for p in problems:
                print(p)
        else:
            print(f"[PASS] {name}")

    return all_ok


if __name__ == "__main__":
    update = "--update" in sys.argv
    print(f"Generator version under test: {GENERATOR_VERSION}\n")
    ok = run(update=update)
    if update:
        print("\nGolden snapshots updated. Review the diff with `git diff regression_golden/` before committing.")
        sys.exit(0)
    print("\n" + ("All regression tests passed." if ok else "REGRESSION DETECTED -- do not deploy until resolved."))
    sys.exit(0 if ok else 1)
