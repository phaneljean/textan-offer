"""Deal Notepad Address Integrity Guard (2026-11-11).

The invariant under test: the Deal Sheet's address slot may only ever
hold a street address. Baptiste's live failure was an extraction that
stored a whole note ("123 Main St ... Buyers John & Jane Doe ... Docs
Still Pending") as the address and printed it across a generated
contract. The guard is one shared shape check
(notepad.looks_like_address) enforced at every door: validate_proposal
drops a failing proposal, apply_enhancement refuses to merge one,
set_field / the fields route reject one with a user-facing message,
and sheet_to_parsed keeps a pre-guard stored blob out of any future
draft (without rewriting the stored field).

    python3 -m unittest tests.test_address_guard
"""
import itertools
import os
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
os.environ["ANALYTICS_PASSWORD"] = "test-internal"
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import notepad  # noqa: E402
import note_enhancer  # noqa: E402
import subscriptions  # noqa: E402

_PHONE_SEQ = itertools.count(700)

BLOB = ("123 Main St, Elm Creek Dr House, . Buyers John & Jane Doe. "
        "Sellers Fine With Dec 18 Close. $. Earnest Maybe ?? , $350 Fee. , . "
        "Fridge + Washer Stay. Asked Seller Credit For The Roof, Not Agreed "
        "Yet. Old Survey From 2019. Docs Still Pending")

BAPTISTE_NOTE = ("123 Main St, Elm Creek Dr house, Austin. Buyers John & Jane Doe. "
                 "Sellers fine with Dec 18 close. $685k. Earnest maybe 7k?? "
                 "10 days option, $350 fee. Conventional, 20% down. "
                 "Fridge + washer stay. Asked 5k seller credit for the roof, "
                 "not agreed yet. Old survey from 2019. HOA docs still pending.")

BOUNDED = "123 Main St, Elm Creek Dr house, Austin"


def _fresh_phone():
    return f"+1514555{next(_PHONE_SEQ):04d}"


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


def _store_fields(phone, tx_id, fields):
    """Write a fields dict straight into the sheet store, bypassing
    every write path -- how a pre-guard sheet (legacy data) is made."""
    notepad.get_deal_sheet(tx_id, phone)  # ensure the row exists
    notepad._write_sheet(tx_id, phone, fields)


class LooksLikeAddress(unittest.TestCase):
    def test_blob_fails(self):
        self.assertFalse(notepad.looks_like_address(BLOB))

    def test_real_addresses_pass(self):
        for addr in ("123 Main St, Elm Creek Dr house, Austin",
                     "11908 Metric Blvd",
                     "88 County Road 12",
                     "123 County Road 12",
                     "742 Evergreen Terrace",
                     "1600 Amphitheatre Parkway",
                     "4500 Commerce St, Apt 12, Dallas",
                     "123 Main St, Austin, TX 78704",
                     "123 Main St.",
                     "1234 FM 1960"):
            self.assertTrue(notepad.looks_like_address(addr), addr)

    def test_non_addresses_fail(self):
        for addr in ("PO Box 123, Austin, TX",      # not a property address
                     "Main St & Elm Dr",            # intersection, no number
                     "123 Main",                    # no street suffix
                     "Austin, TX",                  # city only (its own field)
                     "123 Main St. Austin. TX",     # two periods
                     "123 Main St, Buyers John & Jane Doe",
                     "123 Main St, earnest money held by title",
                     "", None, 123):
            self.assertFalse(notepad.looks_like_address(addr), repr(addr))

    def test_length_cap(self):
        # Address-shaped and uncontaminated, but over 100 characters:
        # only the length rule can refuse it.
        addr = "123 Main St, " + "Unit " * 20 + "Austin"
        self.assertGreater(len(addr), 100)
        self.assertFalse(notepad.looks_like_address(addr))


class ValidatorDropsBlob(unittest.TestCase):
    def test_blob_proposal_dropped_with_warning(self):
        # A future producer (an LLM) hands the validator the whole note
        # as the "address" with a perfectly valid short span: the span
        # gate passes, the shape guard must still drop it.
        fields, warnings = note_enhancer.validate_proposal(
            BAPTISTE_NOTE,
            {"address": {"value": BLOB, "state": "found",
                         "span": "123 Main St", "origin": "llm", "note": ""}},
            "TX")
        self.assertNotIn("address", fields)
        self.assertTrue(any("Address" in w for w in warnings), warnings)

    def test_bounded_address_survives_validation(self):
        result = note_enhancer.enhance_note(BAPTISTE_NOTE, "TX", None)
        fields, _ = note_enhancer.validate_proposal(
            BAPTISTE_NOTE, result["fields"], "TX")
        self.assertEqual(fields["address"]["value"], BOUNDED)


class ApplyEnhancementGuard(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)

    def test_blob_never_lands_even_unvalidated(self):
        # Defense in depth: apply_enhancement is handed the blob
        # directly, with validation skipped entirely.
        note = notepad.add_note(self.tx["id"], self.phone, BAPTISTE_NOTE)
        result = notepad.apply_enhancement(
            self.tx["id"], self.phone, note,
            {"provider": "regex", "fields": {}},
            {"address": {"value": BLOB, "state": "found",
                         "span": "123 Main St", "origin": "llm"}})
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertNotIn("address", fields)
        self.assertTrue(any("Address" in w for w in result["warnings"]),
                        result["warnings"])

    def test_baptiste_note_lands_the_bounded_address(self):
        _apply(self.phone, self.tx["id"], BAPTISTE_NOTE)
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertEqual(fields["address"]["value"], BOUNDED)


class SetFieldGuard(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)

    def test_set_field_rejects_blob_with_message(self):
        with self.assertRaises(ValueError) as ctx:
            notepad.set_field(self.tx["id"], self.phone, "address", BLOB)
        self.assertEqual(str(ctx.exception), notepad.ADDRESS_GUARD_MESSAGE)
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertNotIn("address", fields)

    def test_set_field_accepts_valid_address_any_state(self):
        fields = notepad.set_field(self.tx["id"], self.phone, "address",
                                   "11908 Metric Blvd")
        self.assertEqual(fields["address"]["value"], "11908 Metric Blvd")
        fields = notepad.set_field(self.tx["id"], self.phone, "address",
                                   "11908 Metric Blvd", state="confirm",
                                   origin="user")
        self.assertEqual(fields["address"]["state"], "confirm")

    def test_clearing_address_still_works(self):
        notepad.set_field(self.tx["id"], self.phone, "address",
                          "11908 Metric Blvd")
        fields = notepad.set_field(self.tx["id"], self.phone, "address", None)
        self.assertNotIn("address", fields)


class FieldsRouteGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _post(self, phone, tx_id, payload):
        return self.client.post(f"/notepad/{tx_id}/fields",
                                query_string=_signed(phone), json=payload)

    def test_route_rejects_blob_with_the_guard_message(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        resp = self._post(phone, tx["id"], {"key": "address", "value": BLOB})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         "That doesn't look like a street address — "
                         "keep it to the property address only.")
        fields = notepad.get_deal_sheet(tx["id"], phone)["fields"]
        self.assertNotIn("address", fields)

    def test_route_accepts_valid_address(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        resp = self._post(phone, tx["id"],
                          {"key": "address", "value": "11908 Metric Blvd"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["fields"]["address"]["value"],
                         "11908 Metric Blvd")

    def test_route_accept_action_cannot_firm_a_legacy_blob(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _store_fields(phone, tx["id"], {
            "address": {"value": BLOB, "state": "confirm", "span": "",
                        "origin": "regex", "updated_at": "2026-11-01"}})
        resp = self._post(phone, tx["id"],
                          {"key": "address", "action": "accept"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"],
                         notepad.ADDRESS_GUARD_MESSAGE)


class ParsedExcludesLegacyBlob(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)

    def test_stored_blob_excluded_from_parsed_but_kept_on_sheet(self):
        _store_fields(self.phone, self.tx["id"], {
            "address": {"value": BLOB, "state": "found", "span": "",
                        "origin": "regex", "updated_at": "2026-11-01"},
            "city": {"value": "Austin", "state": "found", "span": "Austin",
                     "origin": "regex", "updated_at": "2026-11-01"}})
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        parsed = notepad.sheet_to_parsed(fields)
        self.assertNotIn("address", parsed)
        self.assertEqual(parsed["city"], "Austin")
        # The guard does not rewrite the stored field itself.
        self.assertEqual(fields["address"]["value"], BLOB)

    def test_stored_valid_address_is_included(self):
        _store_fields(self.phone, self.tx["id"], {
            "address": {"value": "11908 Metric Blvd", "state": "found",
                        "span": "", "origin": "regex",
                        "updated_at": "2026-11-01"}})
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertEqual(notepad.sheet_to_parsed(fields)["address"],
                         "11908 Metric Blvd")


if __name__ == "__main__":
    unittest.main()
