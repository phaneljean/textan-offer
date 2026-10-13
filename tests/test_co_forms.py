"""Colorado companion forms (co_forms.py): Amend/Extend, Counterproposal,
Inspection Objection Notice, Source of Water Addendum -- each cross-checked
against the CBS1, plus CBS1-vs-CBS1 "what changed".

    python3 -m unittest tests.test_co_forms
"""
import os
import tempfile
import unittest

import co_forms
from tc_audit import check_tc_file, compare_contracts
from tests.co_fixtures import fill, fill_form

AE = co_forms.AMEND_CELLS["ae41"]
CP = co_forms.AMEND_CELLS["ctr"]
HEADER = {"Original Contract Date": "10/01/2026", "Street Address": "123 Main St", "City": "Denver"}


class CoForms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.contract = fill(out=cls.p("contract.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def p(cls, name):
        return os.path.join(cls.tmp.name, name)

    def keys(self, *forms):
        r = check_tc_file([self.contract] + list(forms))
        self.assertEqual(r["form"], "Colorado CBS1")
        return sorted(i["key"] for i in r["issues"]), r

    def test_classify_blank_forms(self):
        from pdf_validator import _read_values
        from tests.co_fixtures import FORMS
        for kind, path in FORMS.items():
            self.assertEqual(co_forms.classify(_read_values(path)), kind)

    def test_clean_extension_is_clean(self):
        ae = fill_form("ae41", {**HEADER, AE["Closing Date"]["new"]: "11/06/2026",
                                AE["Possession Date"]["new"]: "11/06/2026"}, self.p("ae_ok.pdf"))
        keys, r = self.keys(ae)
        self.assertEqual(keys, [])
        self.assertEqual(r["co_attachments"], ["Agreement to Amend/Extend"])

    def test_amendment_pushes_deadline_past_closing(self):
        ae = fill_form("ae41", {**HEADER, AE["Inspection Resolution Deadline"]["new"]: "11/03/2026"}, self.p("ae1.pdf"))
        self.assertEqual(self.keys(ae)[0], ["co_amend_co_after_closing_insp_res"])

    def test_amendment_deletes_loan_contingency(self):
        ae = fill_form("ae41", {**HEADER, AE["New Loan Availability Deadline"]["deleted"]: "X"}, self.p("ae2.pdf"))
        self.assertEqual(self.keys(ae)[0], ["co_amend_co_loan_avail_deleted"])

    def test_amendment_row_conflict(self):
        ae = fill_form("ae41", {**HEADER, AE["Appraisal Deadline"]["new"]: "10/21/2026",
                                AE["Appraisal Deadline"]["nochange"]: "X"}, self.p("ae3.pdf"))
        self.assertEqual(self.keys(ae)[0], ["co_amend_row_conflict"])

    def test_amendment_wrong_property(self):
        ae = fill_form("ae41", {**HEADER, "Street Address": "9 Elm St", AE["Closing Date"]["new"]: "11/06/2026",
                                AE["Possession Date"]["new"]: "11/06/2026"}, self.p("ae4.pdf"))
        self.assertEqual(self.keys(ae)[0], ["co_ae41_address_mismatch"])

    def test_empty_amendment(self):
        ae = fill_form("ae41", HEADER, self.p("ae5.pdf"))
        self.assertEqual(self.keys(ae)[0], ["co_ae41_empty"])

    def test_counterproposal_price_change_and_math(self):
        cp = fill_form("ctr", {**HEADER, "Purchase Price": "510,000.00", "Earnest Money": "10,000.00",
                               "New Loan": "400,000.00", "Cash At Closing": "90,000.00",
                               "Total 01": "510,000.00", "Total 02": "500,000.00"}, self.p("cp1.pdf"))
        keys = self.keys(cp)[0]
        self.assertIn("co_amend_price_change", keys)
        self.assertIn("co_amend_co_price_math", keys)   # 10k + 400k + 90k != 510k

    def test_counterproposal_dates(self):
        cp = fill_form("ctr", {**HEADER, CP["Closing Date"]["new"]: "10/31/2026",
                               CP["Possession Date"]["new"]: "10/31/2026"}, self.p("cp2.pdf"))
        self.assertEqual(self.keys(cp)[0], ["co_amend_co_closing_weekend"])  # 10/31/2026 is a Saturday

    def test_inspection_notice_late_and_blank(self):
        n = fill_form("insp", {"Date": "10/14/2026", "Contract Date": "10/01/2026",
                               "Property Name": "123 Main St, Denver, CO 80202"}, self.p("n1.pdf"))
        self.assertEqual(self.keys(n)[0], ["co_insp_description_blank", "co_insp_late"])

    def test_inspection_notice_on_time(self):
        n = fill_form("insp", {"Date": "10/12/2026", "Contract Date": "10/01/2026",
                               "Property Name": "123 Main St, Denver, CO 80202",
                               "Description of What Is Unsatisfactory": "Roof leak at north eave"}, self.p("n2.pdf"))
        self.assertEqual(self.keys(n)[0], [])

    def test_inspection_notice_uses_amended_deadline(self):
        ae = fill_form("ae41", {**HEADER, AE["Inspection Objection Deadline"]["new"]: "10/15/2026",
                                AE["Inspection Termination Deadline"]["new"]: "10/15/2026",
                                AE["Inspection Resolution Deadline"]["new"]: "10/19/2026"}, self.p("ae6.pdf"))
        n = fill_form("insp", {"Date": "10/14/2026", "Contract Date": "10/01/2026",
                               "Property Name": "123 Main St, Denver, CO 80202",
                               "Description of What Is Unsatisfactory": "Roof leak"}, self.p("n3.pdf"))
        self.assertEqual(self.keys(ae, n)[0], [])

    def test_swa_listed_but_missing(self):
        c = fill({"Documents Part of the Contract": "Source of Water Addendum"}, out=self.p("c_swa.pdf"))
        r = check_tc_file([c])
        self.assertIn("co_swa_listed_missing", [i["key"] for i in r["issues"]])
        swa = fill_form("swa", {"Contract Date": "10/01/2026", "Street Address": "123 Main St"}, self.p("swa.pdf"))
        r = check_tc_file([c, swa])
        self.assertNotIn("co_swa_listed_missing", [i["key"] for i in r["issues"]])

    def test_compare_two_cbs1s(self):
        v2 = fill({"Purchase Price": "510,000.00", "Cash At Closing": "100,000.00", "Total 01": "510,000.00",
                   "Total 02": "510,000.00", "Inspection Objection Deadline": "10/14/2026"}, out=self.p("v2.pdf"))
        r = compare_contracts(self.contract, v2)
        changed = {c["field"] for c in r["changes"] if c["status"] == "changed"}
        self.assertEqual(changed, {"Purchase Price", "Cash at Closing", "Inspection Objection Deadline"})

    def test_compare_mixed_forms_refused(self):
        here = os.path.dirname(os.path.abspath(__file__))
        r = compare_contracts(self.contract, os.path.join(here, "fixtures", "trec_20-19_clean_two_parties.pdf"))
        self.assertFalse(r["recognized"])


if __name__ == "__main__":
    unittest.main()


class CoBuyerAgreement(unittest.TestCase):
    """HB26-1426: BC60 Exclusive Right-to-Buy cross-check and BUYER AGREEMENT by text."""
    OK = {"Buyer": "Pat Buyer", "Buyer Agency": True, "Listing Period Starts": "09/15/2026",
          "Listing Period Ends": "12/15/2026", "7.1.1 Checkbox": True, "7.1.1.1 Percentage": "2.5"}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.contract = fill({"Broker Working With Buyer Buyers Agent Checkbox": True}, out=cls.p("c_agent.pdf"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def p(cls, name):
        return os.path.join(cls.tmp.name, name)

    def keys(self, *files):
        return sorted(i["key"] for i in check_tc_file([self.contract] + list(files))["issues"])

    def ertb(self, name, **over):
        vals = dict(self.OK)
        vals.update(over)
        return fill_form("ertb", {k: v for k, v in vals.items() if v is not None}, self.p(name))

    def test_agent_without_agreement_is_flagged(self):
        self.assertEqual(self.keys(), ["co_ertb_not_attached"])

    def test_customer_relationship_not_flagged(self):
        c = fill({"Broker Working With Buyer Customer Checkbox": True}, out=self.p("c_cust.pdf"))
        self.assertEqual(check_tc_file([c])["issues"], [])

    def test_good_agreement_is_clean(self):
        self.assertEqual(self.keys(self.ertb("ok.pdf")), [])

    def test_agreement_expired_before_contract(self):
        f = self.ertb("exp.pdf", **{"Listing Period Ends": "09/30/2026"})
        issues = check_tc_file([self.contract, f])["issues"]
        self.assertEqual([(i["key"], i["severity"]) for i in issues], [("co_ertb_period_outside", "warning")])

    def test_adjusted_amount_other_is_not_a_fee(self):
        f = self.ertb("adj.pdf", **{"7.1.1.1 Percentage": None, "7.1.1.1 Other Description": "see 23"})
        self.assertIn("co_ertb_comp_blank", self.keys(f))

    def test_agreement_buyer_mismatch_and_no_fee(self):
        f = self.ertb("mm.pdf", Buyer="Chris Other", **{"7.1.1.1 Percentage": None, "7.1.1 Checkbox": None})
        self.assertEqual(self.keys(f), ["co_ertb_buyer_mismatch", "co_ertb_comp_blank"])

    def test_relationship_mismatch(self):
        f = self.ertb("rel.pdf", **{"Buyer Agency": None, "Transaction Brokerage": True})
        self.assertEqual(self.keys(f), ["co_ertb_relationship_mismatch"])

    def test_text_command_fills_form(self):
        from datetime import date
        from co_filler import parse_buyer_agreement_sms, fill_buyer_agreement
        from pdf_validator import _read_values
        import pdf_filler
        parsed = parse_buyer_agreement_sms("BUYER AGREEMENT Pat Buyer 2.5% 90 days agency")
        old = pdf_filler.OUTPUT_DIR
        try:
            import co_filler
            co_filler.OUTPUT_DIR = self.tmp.name
            path = fill_buyer_agreement(parsed, {"name": "Jane Agent", "brokerage": "Example Realty"},
                                        today=date(2026, 9, 20))
        finally:
            co_filler.OUTPUT_DIR = old
        v = _read_values(path)
        self.assertEqual(co_forms.classify(v), "ertb")
        self.assertEqual((v["Buyer"], v["7.1.1.1 Percentage"], v["Listing Period Ends"]), ("Pat Buyer", "2.5", "12/19/2026"))
        self.assertEqual(self.keys(path), [])   # the generated agreement passes its own check
        for bad in ("BUYER AGREEMENT Pat Buyer 90 days", "BUYER AGREEMENT Pat Buyer 2.5%", "BUYER AGREEMENT 2.5% 90 days"):
            self.assertIn("error", parse_buyer_agreement_sms(bad))
