"""Deal Notepad enhancer (2026-11-11): the deterministic producer and
the validation gate. The enhancer's whole contract is "propose, never
invent": every field is a Confirm-state proposal whose span is an exact
substring of the raw note, and validate_proposal drops or demotes
anything a future producer (an LLM) makes up.

    python3 -m unittest tests.test_note_enhancer
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")

import note_enhancer  # noqa: E402
import notepad  # noqa: E402

SAMPLE = ("Pecan Grove condo, buyers John & Jane Doe, 685k, earnest maybe 7k ??, "
          "10 day option $350, signed Nov 10, close Dec 18, fridge + washer stay, "
          "roof credit 5k, 20% down, Lake Travis Title")


def _norm(s):
    return " ".join(str(s or "").split()).lower()


class DeterministicExtraction(unittest.TestCase):
    def setUp(self):
        self.result = note_enhancer.enhance_note(SAMPLE, "TX", None)
        self.fields = self.result["fields"]

    def test_status_and_empty_summary(self):
        # The regex producer must identify itself and must not fake a summary.
        self.assertEqual(self.result["status"], "fallback_regex")
        self.assertEqual(self.result["provider"], "regex")
        self.assertEqual(self.result["summary"], "")

    def test_core_values(self):
        f = self.fields
        self.assertEqual(f["price"]["value"], 685000)
        self.assertEqual(f["earnest_money_amount"]["value"], 7000)
        self.assertEqual(f["option_fee_amount"]["value"], 350)
        self.assertEqual(f["option_days"]["value"], 10)
        self.assertEqual(f["down_payment_pct"]["value"], 0.2)
        self.assertEqual(f["buyer_name"]["value"], "John & Jane Doe")
        self.assertEqual(f["inclusions"]["value"], "fridge + washer stay")
        self.assertIn("closing_date", f)
        self.assertIn("effective_date", f)

    def test_everything_is_a_confirm_proposal(self):
        for key, field in self.fields.items():
            self.assertEqual(field["state"], "confirm", key)
            self.assertEqual(field["origin"], "regex", key)
            self.assertIn("note", field, key)

    def test_every_span_is_an_exact_substring(self):
        for key, field in self.fields.items():
            self.assertIn(_norm(field["span"]), _norm(SAMPLE), key)

    def test_credit_amount_is_not_the_price(self):
        # "roof credit 5k" is a concession, never the sales price.
        self.assertNotEqual(self.fields["price"]["value"], 5000)
        self.assertNotEqual(self.fields["price"]["span"], "5k")

    def test_hedge_note_scoped_to_the_hedged_clause(self):
        # "earnest maybe 7k ??" is hedged; "signed Nov 10" is not, even
        # though both live in the same comma-separated note.
        self.assertIn("hedge", self.fields["earnest_money_amount"]["note"])
        self.assertEqual(self.fields["effective_date"]["note"], "")
        self.assertEqual(self.fields["option_days"]["note"], "")

    def test_recent_past_date_stays_this_year(self):
        two_days_ago = datetime.now().date() - timedelta(days=2)
        note = f"sellers: The Nguyen Family, 512k, signed {two_days_ago:%b} {two_days_ago.day}"
        fields = note_enhancer.enhance_note(note, "TX", None)["fields"]
        self.assertEqual(fields["effective_date"]["value"],
                         two_days_ago.isoformat())

    def test_multiple_prices_warn_and_take_first(self):
        fields = note_enhancer.enhance_note(
            "listed 685k but they offered 640k, earnest 7k", "TX", None)["fields"]
        self.assertEqual(fields["price"]["value"], 685000)
        self.assertIn("more than one price", fields["price"]["note"])


class LiveNoteRegressions(unittest.TestCase):
    """Baptiste's own live test note (2026-11-11) -- the four misses the
    mechanical enhancer made on real phrasing: the address swallowed the
    note, sentence-initial "Buyers" found no buyer, "Dec 18 close" found
    no closing date, and "10 days option" became close_days."""

    NOTE = ("123 Main St, Elm Creek Dr house, Austin. Buyers John & Jane Doe. "
            "Sellers fine with Dec 18 close. $685k. Earnest maybe 7k?? "
            "10 days option, $350 fee. Conventional, 20% down. "
            "Fridge + washer stay. Asked 5k seller credit for the roof, "
            "not agreed yet. Old survey from 2019. HOA docs still pending.")

    def setUp(self):
        self.fields = note_enhancer.enhance_note(self.NOTE, "TX", None)["fields"]

    def test_address_is_bounded_to_the_address(self):
        value = self.fields["address"]["value"]
        self.assertIn("123 Main St", value)
        self.assertNotIn("Buyers", value)
        self.assertNotIn("Doe", value)
        self.assertNotIn("Earnest", value)

    def test_sentence_initial_buyers_found(self):
        self.assertEqual(self.fields["buyer_name"]["value"], "John & Jane Doe")

    def test_date_first_closing_date_found(self):
        value = self.fields["closing_date"]["value"]
        _year, month, day = (int(p) for p in value.split("-"))
        self.assertEqual((month, day), (12, 18))

    def test_option_days_bind_and_close_days_absent(self):
        self.assertEqual(self.fields["option_days"]["value"], 10)
        self.assertNotIn("close_days", self.fields)

    def test_working_extractions_not_regressed(self):
        f = self.fields
        self.assertEqual(f["price"]["value"], 685000)
        self.assertEqual(f["earnest_money_amount"]["value"], 7000)
        self.assertIn("hedge", f["earnest_money_amount"]["note"])
        self.assertEqual(f["down_payment_pct"]["value"], 0.2)
        self.assertEqual(f["financing_type"]["value"], "conventional")
        self.assertEqual(f["inclusions"]["value"], "Fridge + washer stay")
        self.assertEqual(f["hoa"]["value"], "HOA docs still pending")
        self.assertIn("seller credit", f["seller_concessions"]["value"])

    def test_every_span_is_an_exact_substring(self):
        for key, field in self.fields.items():
            self.assertIn(_norm(field["span"]), _norm(self.NOTE), key)

    def test_option_period_phrasing_and_real_close_phrase_coexist(self):
        fields = note_enhancer.enhance_note(
            "7 day option period, close in 30 days, 500k", "TX", None)["fields"]
        self.assertEqual(fields["option_days"]["value"], 7)
        self.assertEqual(fields["close_days"]["value"], 30)


class ValidationGate(unittest.TestCase):
    def test_invented_money_field_is_dropped(self):
        fields, warnings = note_enhancer.validate_proposal(
            "nice house, call the seller tomorrow",
            {"price": {"value": 500000, "state": "found",
                       "span": "500k", "origin": "llm", "note": ""}}, "TX")
        self.assertNotIn("price", fields)
        self.assertTrue(any("Dropped" in w for w in warnings))

    def test_free_text_bad_span_demoted_when_value_verbatim(self):
        note = "seller will fix the roof before closing, 400k"
        fields, _ = note_enhancer.validate_proposal(
            note,
            {"seller_concessions": {"value": "seller will fix the roof",
                                    "state": "found", "span": "roof stuff",
                                    "origin": "llm", "note": ""}}, "TX")
        self.assertEqual(fields["seller_concessions"]["state"], "confirm")

    def test_free_text_invented_value_dropped(self):
        fields, warnings = note_enhancer.validate_proposal(
            "call the seller tomorrow",
            {"special_notes": {"value": "pool included", "state": "confirm",
                               "span": "pool", "origin": "llm", "note": ""}}, "TX")
        self.assertNotIn("special_notes", fields)

    def test_out_of_range_pct_demoted_not_fixed(self):
        fields, warnings = note_enhancer.validate_proposal(
            "20% down on the place",
            {"down_payment_pct": {"value": 5.0, "state": "found",
                                  "span": "20% down", "origin": "llm",
                                  "note": ""}}, "TX")
        self.assertEqual(fields["down_payment_pct"]["value"], 5.0)
        self.assertEqual(fields["down_payment_pct"]["state"], "confirm")
        self.assertTrue(warnings)

    def test_closing_before_effective_demotes_both(self):
        note = "signed Dec 18, close Nov 10"
        fields, warnings = note_enhancer.validate_proposal(
            note,
            {"effective_date": {"value": "2026-12-18", "state": "found",
                                "span": "signed Dec 18", "origin": "llm", "note": ""},
             "closing_date": {"value": "2026-11-10", "state": "found",
                              "span": "close Nov 10", "origin": "llm", "note": ""}},
            "TX")
        self.assertEqual(fields["effective_date"]["state"], "confirm")
        self.assertEqual(fields["closing_date"]["state"], "confirm")
        self.assertTrue(any("before the effective date" in w for w in warnings))

    def test_colorado_drops_texas_only_fields(self):
        note = "Denver condo, 720k, 10 day option $350"
        fields, _ = note_enhancer.validate_proposal(
            note,
            {"option_days": {"value": 10, "state": "confirm",
                             "span": "10 day option", "origin": "regex", "note": ""},
             "option_fee_amount": {"value": 350, "state": "confirm",
                                   "span": "option $350", "origin": "regex", "note": ""},
             "price": {"value": 720000, "state": "confirm",
                       "span": "720k", "origin": "regex", "note": ""}}, "CO")
        self.assertNotIn("option_days", fields)
        self.assertNotIn("option_fee_amount", fields)
        self.assertIn("price", fields)

    def test_unknown_keys_dropped(self):
        fields, _ = note_enhancer.validate_proposal(
            "685k for the condo",
            {"pool_size": {"value": 3, "state": "confirm", "span": "685k",
                           "origin": "llm", "note": ""}}, "TX")
        self.assertEqual(fields, {})


if __name__ == "__main__":
    unittest.main()
