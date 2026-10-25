"""The sample report's "Why it matters" lines and form pages (2026-10-25):
every finding on both sample reports has both, the findings themselves are
still the real engine's output, and every page is the page the checked
field's widget actually sits on in the sample PDF -- not a guess.

    python3 -m unittest tests.test_why_it_matters
"""
import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

from pypdf import PdfReader

import app as app_module
import co_audit
import tc_audit
from pdf_filler import FIELD_MAP
from tc_audit import check_tc_file
from tc_check_email import (_FINDING_PAGES, _WHY_IT_MATTERS, _clean, _display_issues, _finding_page)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = {"tx": os.path.join(ROOT, "static", "sample_trec_20-19.pdf"),
           "co": os.path.join(ROOT, "static", "sample_cbs1.pdf")}


def _check(path, demo=True):
    data = {"file": (open(path, "rb"), os.path.basename(path)), "source_page": "claude_verify"}
    if demo:
        data["is_demo"] = "1"
    r = app_module.app.test_client().post("/v1/tc/check", data=data, content_type="multipart/form-data")
    data["file"][0].close()
    return r.get_json()


def _widget_pages(path):
    """{field /T name: {1-based page}} from where each widget really is."""
    out = {}
    for n, page in enumerate(PdfReader(path).pages, 1):
        for a in page.get("/Annots") or []:
            o = a.get_object()
            t = o.get("/T")
            if t is None and o.get("/Parent") is not None:
                t = o["/Parent"].get_object().get("/T")
            if t is not None:
                out.setdefault(str(t), set()).add(n)
    return out


# The fields each check reads, straight from the engine's own tables.
_B = tc_audit.BROKER_CONTRIB
TX_FIELDS = {
    "sales_price_math": [FIELD_MAP[k] for k in ("down_payment", "loan_amount", "sales_price")],
    "closing_date_invalid": [tc_audit.SLOTS["closing_date"][1], tc_audit.SLOTS["closing_year"][1]],
    "option_days_blank": [FIELD_MAP["option_fee_amount"], tc_audit.SLOTS["option_days"][1]],
    ("check_one_conflict", "Paragraph 6C"): [b for _, b in tc_audit.SURVEY_BOXES],
    ("check_one_conflict", "Paragraph 7B"): [b for _, b in tc_audit.DISCLOSURE_BOXES],
    ("check_one_conflict", "Paragraph 7D"): [b for _, b in tc_audit.AS_IS_BOXES],
    ("check_one_conflict", "Paragraph 6E(2)"): [b for _, b in tc_audit.POA_BOXES],
    ("check_one_conflict", "Paragraph 10A"): [b for _, b in tc_audit.POSSESSION_BOXES],
    "disclosure_days_blank": [tc_audit.DISCLOSURE_BOXES[1][1], tc_audit.SLOTS["disclosure_days"][1]],
    "broker_contribution_incomplete": list(_B[0][1:]) + list(_B[1][1:]),
    "poa_addendum_missing": [tc_audit.POA_BOXES[0][1], FIELD_MAP["hoa_addendum"]],
    "receipt_mismatch": [f for _, r, k, _ in tc_audit.RECEIPTS for f in (r, FIELD_MAP[k])],
    "special_provisions_business_term": tc_audit.SPECIAL_PROVISIONS,
    "email_invalid": [n for _, n in tc_audit.NOTICE_EMAILS],
    "financing_addendum_missing": [FIELD_MAP["third_party_financing_3b"], FIELD_MAP["third_party_financing"]],
}
CO_FIELDS = {
    "co_price_math": [co_audit.F[k] for k in ("price", "earnest", "new_loan", "assumption", "private", "seller_fin", "cash")],
    "co_loan_avail_deleted": [co_audit.DEADLINES["loan_avail"][0], co_audit.F["new_loan"]],
    "co_closing_holiday": [co_audit.DEADLINES["closing"][0]],
}


class SampleReportWhyItMatters(unittest.TestCase):
    def test_every_sample_finding_has_page_and_why(self):
        for state, path in SAMPLES.items():
            shown = _check(path)["display_issues"]
            self.assertTrue(shown, state)
            for i in shown:
                self.assertRegex(i.get("page", ""), r"^Pages? \d+((, | and )\d+)* of %d$" % (12 if state == "tx" else 20),
                                 (state, i["message"]))
                self.assertTrue(i.get("why", "").strip(), (state, i["message"]))

    def test_findings_are_still_the_engine_output(self):
        for state, path in SAMPLES.items():
            real = [(i["severity"], _clean(i["message"])) for i in _display_issues(check_tc_file([path])["issues"])]
            real.sort(key=lambda r: r[0] != "blocker")
            self.assertEqual([(i["severity"], i["message"]) for i in _check(path)["display_issues"]], real, state)

    def test_real_reports_unchanged(self):
        for path in SAMPLES.values():
            for i in _check(path, demo=False)["display_issues"]:
                self.assertNotIn("why", i)
                self.assertNotIn("page", i)

    def test_pages_match_widget_positions(self):
        for path, table in ((SAMPLES["tx"], TX_FIELDS), (SAMPLES["co"], CO_FIELDS)):
            pages = _widget_pages(path)
            for spec, fields in table.items():
                key, prefix = spec if isinstance(spec, tuple) else (spec, None)
                want = set()
                for f in fields:
                    self.assertIn(f, pages, (key, f))
                    want |= pages[f]
                got = _FINDING_PAGES[key][prefix] if prefix else _FINDING_PAGES[key]
                self.assertEqual(set(got), want, (key, prefix))
            # and nothing in the page table goes unchecked here
            for key, v in _FINDING_PAGES.items():
                if (key.startswith("co_")) == (table is CO_FIELDS):
                    specs = [(key, p) for p in v] if isinstance(v, dict) else [key]
                    for s in specs:
                        self.assertIn(s, table, s)

    def test_pages_named_in_the_message_are_used(self):
        self.assertEqual(_finding_page({"key": "initials_buyer", "message": "Buyer initials missing on pages 1, 4 and 7 of 12"}),
                         "Pages 1, 4 and 7 of 12")
        self.assertEqual(_finding_page({"key": "header_address_blank", "message": 'Page 5 of 12: "Address of Property" header is blank'}),
                         "Page 5 of 12")
        self.assertEqual(_finding_page({"key": "not_listed", "message": "x"}), "")

    def test_why_lines_stay_measured(self):
        # One sentence each, no legal conclusions or alarm words (trec-qa rules).
        banned = re.compile(r"\b(void|voids|unenforceable|illegal|guarantee\w*|always|never|urgent|free)\b|!", re.I)
        for key, why in _WHY_IT_MATTERS.items():
            self.assertFalse(banned.search(why), (key, why))
            self.assertEqual(why.count(". "), 0, key)
            self.assertTrue(why.endswith("."), key)

    def test_sample_label_and_renderer(self):
        html = app_module.app.test_client().get("/tc-check").get_data(as_text=True)
        self.assertIn("a sample contract with planted mistakes, not your file.", html)
        self.assertIn("Why it matters:", html)
        self.assertIn('class="issue-page"', html)


if __name__ == "__main__":
    unittest.main()
