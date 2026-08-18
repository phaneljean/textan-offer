# TxtAnOffer — PDF Generation QA Spec

**Purpose:** Permanent reference for verifying that every generated TREC contract PDF is complete, accurate, and safe to send to a real listing agent — before it ships, and after every code change to the generator.

---

## 1. Automated Pre-Send Validation (build this first)

Before the "Email to Listing Agent" action is enabled on any generated offer, the system must programmatically verify the following fields are non-empty and internally consistent. If any check fails, **block sending** and show the sender exactly which field(s) are missing or inconsistent — never let a partial document reach "ready to send" silently.

### Required non-empty fields

| Section | Field(s) |
|---|---|
| 1. Parties | Buyer name, Seller name |
| 2A. Property | City, County, Address |
| 3. Sales Price | Cash portion (A), Financing sum (B), Total (C) |
| 5. Earnest Money | Escrow Agent, Escrow Agent address, Earnest Money $, Option Fee $ |
| 6A. Title Policy | Title Company |
| 9. Closing | Closing Date (must appear only in Section 9, nowhere else) |
| 21. Notices | Buyer's agent: Address, Phone, Email (minimum — Seller side may be legitimately blank at draft stage) |
| 40-11 §1 | Financing type checkbox checked + matching principal amount |
| 40-11 §A | Buyer Approval — exactly one box checked |

### Consistency checks

- Financing checkbox in **Section 3B** and **Section 22** must both be checked whenever a financing addendum is attached.
- The addendum checked in **Section 22** must match the actual financing type selected (e.g., don't check the VA/assumption release addendum unless financing type is VA or Loan Assumption).
- Every dollar figure and date shown on the **summary card** must match the corresponding value in the **PDF form body** exactly. This is the single highest-value automated check — it would have caught the worst bug found during testing (form body generating blank while summary card generated correctly).
- No numeric value should appear in a non-numeric field (e.g., a dollar amount landing on an "Initialed by Buyer/Seller" line).

---

## 2. Regression Test Routine (run after every generator code change)

Maintain 2–3 fixed test addresses with known-good expected output. After any change to the PDF generation pipeline:

1. Regenerate all test addresses.
2. Diff the new output against the last known-good PDF, field by field.
3. Confirm no previously-passing field has regressed to blank, scrambled, or misplaced.
4. Only merge/deploy if the diff is clean or the only changes are the intended fix.

This is not optional cleanup — it is what catches pipeline-wide failures (like the empty-form-body bug) before they reach a paying customer. Treat it as a required step, same category as running a test suite before deploy.

---

## 3. Manual Review Checklist (interim — until Section 1 is fully automated)

Before any real offer goes out, scan for:

- [ ] All dollar amounts match across the summary card, Section 3, and the 40-11 addendum
- [ ] Financing checkbox + Section 22 addendum checkbox both checked and consistent with financing type
- [ ] Buyer Approval section — exactly one box checked, matches actual deal terms
- [ ] Escrow Agent / Title Company are real values, not leftover placeholder text
- [ ] Buyer's agent block — name, license, phone, email, address all present in **both** Section 21 and the Broker Contact Information page
- [ ] No stray values bleeding into wrong fields (e.g., dollar amounts on initial lines)
- [ ] Closing date appears once, in Section 9, and matches the summary card
- [ ] City/state combination is valid (e.g., doesn't silently pass a mismatched city/state pair)

---

## 4. Operational Safeguards

- **Version tagging:** Every generated PDF should carry a generator-version identifier (visible or in metadata) so that if a bug is later discovered, affected offers can be identified and the sender notified.
- **Audit log:** Maintain a running log (even a simple spreadsheet) of real offers sent — address, date, generator version, and a pass/fail note from review. This turns "did a bad contract ever go out" into an answerable question rather than a guess.
- **Dashboard/profile data:** Agent profile fields (name, firm, license, phone, email, business address) should be entered once and reused everywhere they're needed on the contract (Section 21, Broker Contact page, etc.) — never re-collected or re-mapped per generation.

---

## 5. Known-fixed issues (for regression reference)

These were confirmed bugs during initial testing and should remain fixed — include them in the regression test's expected-good criteria:

- PDF form body failing to populate while summary card populated correctly (root cause: form-fill target mismatch, likely tied to a TREC template version change)
- Closing date misfiring into Section 12A instead of Section 9
- Broker/Intermediary contact fields mapped off-by-one (license number in address field, etc.)
- Wrong addendum (VA/assumption release) auto-checked regardless of actual financing type
- Buyer's agent contact info (address specifically) missing from both Section 21 and Broker Contact page due to a missing profile field
- Dollar values leaking into "Initialed by Buyer/Seller" signature lines

---

*This document should be treated as a living spec — update it whenever a new bug class is discovered or a new safeguard is added.*
