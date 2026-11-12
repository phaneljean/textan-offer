"""Deal Notepad Accuracy Pass (2026-11-11): the three-status Deal Sheet.

The invariants under test: a profile default is a standing preference,
not a fact about the deal -- it lands as Confirm with a verify note and
blocks a draft until the user firms it (Baptiste's live scenario: the
$250 option fee). A value derived from Captured inputs is stored as
Calculated with its formula, exists only while its inputs stay
Captured, and converts to Captured on a user edit. Seller concessions
whose words say "not agreed" are labeled "Proposed, not agreed." The
draft gate refuses on Confirm fields (blocking) and on required fields
missing entirely (missing), with deliberate exemptions only for fields
the filler never writes (effective date, party names, escrow agent).

    python3 -m unittest tests.test_notepad_accuracy
"""
import itertools
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import deadlines  # noqa: E402
import notepad  # noqa: E402
import note_enhancer  # noqa: E402
import subscriptions  # noqa: E402

_PHONE_SEQ = itertools.count(900)

BAPTISTE_NOTE = ("123 Main St, Elm Creek Dr house, Austin. Buyers John & Jane Doe. "
                 "Sellers fine with Dec 18 close. $685k. Earnest maybe 7k?? "
                 "10 days option, $350 fee. Conventional, 20% down. "
                 "Fridge + washer stay. Asked 5k seller credit for the roof, "
                 "not agreed yet. Old survey from 2019. HOA docs still pending.")

PROFILE = {"default_option_fee": 250, "default_earnest_pct": 0.01,
           "title_company": "FirstChoice Title"}


def _fresh_phone():
    return f"+1514999{next(_PHONE_SEQ):04d}"


def _mkuser(phone, plan=None):
    subscriptions.create_user(phone)
    if plan:
        subscriptions.activate_subscription(phone, "cus_t", "sub_t", plan)
    return phone


def _signed(phone):
    url = app_module.sign_dashboard_url(phone, "http://localhost")
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def _apply(phone, tx_id, note_text, profile=None):
    note = notepad.add_note(tx_id, phone, note_text)
    result = note_enhancer.enhance_note(note_text, "TX", profile)
    validated, vw = note_enhancer.validate_proposal(note_text, result["fields"], "TX")
    return notepad.apply_enhancement(tx_id, phone, note, result, validated,
                                     validation_warnings=vw, profile=profile)


def _firm(phone, tx_id, key, value):
    return notepad.set_field(tx_id, phone, key, value,
                             state="found", origin="user")


def _full_sheet(phone, tx_id, skip=()):
    """Every draft-required field firmed, mirroring phases23's sheet,
    minus any keys in `skip`."""
    today = deadlines.texas_date()
    values = {
        "address": "123 Main St, Austin, TX 78704",
        "city": "Austin",
        "county": "Travis",
        "buyer_name": "John Doe",
        "seller_name": "Jane Roe",
        "price": 500000,
        "down_payment_pct": 0.2,
        "financing_type": "conventional",
        "earnest_money_amount": 5000,
        "effective_date": today.isoformat(),
        "close_days": 30,
        "closing_date": (today + timedelta(days=30)).isoformat(),
        "option_fee_amount": 250,
        "option_days": 10,
        "title_company": "Lake Travis Title",
    }
    for key, value in values.items():
        if key not in skip:
            _firm(phone, tx_id, key, value)


class ProfileDefaultsAreConfirm(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)

    def test_defaults_land_confirm_with_verify_note(self):
        _apply(self.phone, self.tx["id"], "quiet deal, nothing to report yet",
               profile=PROFILE)
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        for key in ("option_fee_amount", "title_company", "earnest_pct"):
            f = fields[key]
            self.assertEqual(f["state"], "confirm", key)
            self.assertEqual(f["origin"], "profile_default", key)
            self.assertEqual(f["span"], "", key)
            self.assertIn("profile default", f["note"].lower(), key)
            self.assertIn("verify", f["note"].lower(), key)
        self.assertEqual(fields["option_fee_amount"]["value"], 250)

    def test_baptiste_scenario_default_fee_blocks_draft(self):
        # The fee arrives ONLY as a profile default; the user firms
        # everything else, including the option days. The draft must
        # still refuse, naming the fee.
        _apply(self.phone, self.tx["id"], "quiet deal, nothing to report yet",
               profile=PROFILE)
        _full_sheet(self.phone, self.tx["id"], skip=("option_fee_amount",))
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertEqual(fields["option_fee_amount"]["state"], "confirm")
        self.assertEqual(fields["option_days"]["state"], "found")
        q = _signed(self.phone)
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "fill_offer_pdf") as fill:
            resp = client.post(f"/notepad/{self.tx['id']}/draft",
                               query_string=q, json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Option fee", resp.get_json()["blocking"])
        fill.assert_not_called()


class CalculatedFields(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)

    def _fields(self):
        return notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]

    def test_earnest_pct_derives_with_formula(self):
        _firm(self.phone, self.tx["id"], "price", 685000)
        _firm(self.phone, self.tx["id"], "earnest_money_amount", 7000)
        pct = self._fields()["earnest_pct"]
        self.assertEqual(pct["state"], "calculated")
        self.assertEqual(pct["origin"], "calculated")
        self.assertEqual(pct["value"], 0.0102)
        self.assertEqual(pct["formula"], "1.02% = $7,000 ÷ $685,000")

    def test_down_payment_amount_derives_with_formula(self):
        _firm(self.phone, self.tx["id"], "price", 685000)
        _firm(self.phone, self.tx["id"], "down_payment_pct", 0.2)
        amt = self._fields()["down_payment_amount"]
        self.assertEqual(amt["state"], "calculated")
        self.assertEqual(amt["value"], 137000)
        self.assertEqual(amt["formula"], "$137,000 = 20% × $685,000")

    def test_down_payment_pct_derives_and_serializes(self):
        _firm(self.phone, self.tx["id"], "price", 500000)
        _firm(self.phone, self.tx["id"], "down_payment_amount", 100000)
        pct = self._fields()["down_payment_pct"]
        self.assertEqual(pct["state"], "calculated")
        self.assertEqual(pct["value"], 0.2)
        parsed = notepad.sheet_to_parsed(self._fields())
        self.assertEqual(parsed["down_payment_pct"], 0.2)

    def test_derivation_withdraws_when_input_unfirmed(self):
        _firm(self.phone, self.tx["id"], "price", 685000)
        _firm(self.phone, self.tx["id"], "earnest_money_amount", 7000)
        self.assertIn("earnest_pct", self._fields())
        notepad.set_field(self.tx["id"], self.phone, "earnest_money_amount",
                          7000, state="confirm", origin="regex")
        fields = self._fields()
        self.assertNotIn("earnest_pct", fields)
        # ...and the gate now sees the unconfirmed earnest itself.
        self.assertIn("earnest_money_amount",
                      notepad.blocking_confirm_fields(fields))

    def test_user_edit_converts_calculated_to_captured(self):
        _firm(self.phone, self.tx["id"], "price", 685000)
        _firm(self.phone, self.tx["id"], "earnest_money_amount", 7000)
        self.assertEqual(self._fields()["earnest_pct"]["state"], "calculated")
        _firm(self.phone, self.tx["id"], "earnest_pct", 0.015)
        pct = self._fields()["earnest_pct"]
        self.assertEqual(pct["state"], "found")
        self.assertEqual(pct["origin"], "user")
        # The user's number wins over math from here on.
        _firm(self.phone, self.tx["id"], "earnest_money_amount", 9000)
        self.assertEqual(self._fields()["earnest_pct"]["value"], 0.015)
        self.assertEqual(self._fields()["earnest_pct"]["state"], "found")

    def test_close_days_derives_from_closing_date(self):
        today = deadlines.texas_date()
        closing = today + timedelta(days=30)
        _firm(self.phone, self.tx["id"], "closing_date", closing.isoformat())
        cd = self._fields()["close_days"]
        self.assertEqual(cd["state"], "calculated")
        self.assertEqual(cd["value"], 30)
        self.assertTrue(cd["formula"].startswith("30 days = "))
        self.assertEqual(notepad.sheet_to_parsed(self._fields())["close_days"], 30)
        notepad.set_field(self.tx["id"], self.phone, "closing_date", None)
        self.assertNotIn("close_days", self._fields())


class ProposedConcessions(unittest.TestCase):
    def test_enhancer_labels_not_agreed_credit(self):
        fields = note_enhancer.enhance_note(BAPTISTE_NOTE, "TX", None)["fields"]
        f = fields["seller_concessions"]
        self.assertEqual(f["state"], "confirm")
        self.assertEqual(f["note"], "Proposed, not agreed.")

    def test_validator_demotes_found_concession_with_markers(self):
        note = "Asked 5k seller credit for the roof, not agreed yet. 400k house."
        fields, _ = note_enhancer.validate_proposal(
            note,
            {"seller_concessions": {
                "value": "seller credit for the roof, not agreed yet",
                "state": "found",
                "span": "seller credit for the roof, not agreed yet",
                "origin": "llm", "note": ""}}, "TX")
        f = fields["seller_concessions"]
        self.assertEqual(f["state"], "confirm")
        self.assertEqual(f["note"], "Proposed, not agreed.")


class DraftGateMissing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _draft(self, phone, tx_id):
        q = _signed(phone)
        with mock.patch.object(app_module, "fill_offer_pdf") as fill:
            resp = self.client.post(f"/notepad/{tx_id}/draft",
                                    query_string=q, json={})
        return resp, fill

    def test_missing_county_refused(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _full_sheet(phone, tx["id"], skip=("county",))
        resp, fill = self._draft(phone, tx["id"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("County", resp.get_json()["missing"])
        fill.assert_not_called()

    def test_missing_title_company_refused_tx(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _full_sheet(phone, tx["id"], skip=("title_company",))
        resp, fill = self._draft(phone, tx["id"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Title company", resp.get_json()["missing"])
        fill.assert_not_called()

    def test_missing_option_days_refused_tx(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _full_sheet(phone, tx["id"], skip=("option_days",))
        resp, fill = self._draft(phone, tx["id"])
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Option days", resp.get_json()["missing"])
        fill.assert_not_called()

    def test_calculated_inputs_satisfy_the_draft(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        # Earnest amount and down payment amount are NEVER entered:
        # both must arrive as Calculated from the firmed percentages.
        _full_sheet(phone, tx["id"],
                    skip=("earnest_money_amount", "down_payment_pct",
                          "down_payment_amount"))
        _firm(phone, tx["id"], "earnest_pct", 0.01)
        _firm(phone, tx["id"], "down_payment_pct", 0.2)
        fields = notepad.get_deal_sheet(tx["id"], phone)["fields"]
        self.assertEqual(fields["earnest_money_amount"]["state"], "calculated")
        self.assertEqual(fields["down_payment_amount"]["state"], "calculated")
        q = _signed(phone)
        captured = {}

        def fake_fill(parsed, source):
            captured["parsed"] = parsed
            path = os.path.join(os.environ["OFFER_OUTPUT_DIR"], "offer_acc.pdf")
            with open(path, "wb") as fh:
                fh.write(b"%PDF-1.4 draft")
            return path

        with mock.patch.object(app_module, "fill_offer_pdf", fake_fill), \
                mock.patch.object(app_module, "check_tc_file",
                                  lambda paths: {"recognized": True, "issues": []}):
            resp = self.client.post(f"/notepad/{tx['id']}/draft",
                                    query_string=q, json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["parsed"]["earnest_money"], 5000)
        self.assertEqual(captured["parsed"]["down_payment_amount"], 100000)


class ThreeStatusRendering(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_detail_page_shows_three_tags_and_formula(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _firm(phone, tx["id"], "price", 685000)
        _firm(phone, tx["id"], "earnest_money_amount", 7000)
        resp = self.client.get(f"/notepad/{tx['id']}",
                               query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Captured", resp.data)
        self.assertIn(b"Calculated", resp.data)
        self.assertIn(b"tag-calc", resp.data)
        self.assertIn("1.02% = $7,000 ÷ $685,000".encode(), resp.data)


if __name__ == "__main__":
    unittest.main()
