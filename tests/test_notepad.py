"""Deal Notepad (2026-11-11): data model, entitlement, and dashboard
routes. The invariants under test are the product's doctrine: notes are
append-only and verbatim, a transaction id never leaks across owners,
enhancements never overwrite what the user firmed, deletes are soft
with a 30-day purge, and every notepad URL carries the signed-link trio
(phone/expires/sig).

    python3 -m unittest tests.test_notepad
"""
import os
import sqlite3
import tempfile
import unittest
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
import notepad  # noqa: E402
import note_enhancer  # noqa: E402
import subscriptions  # noqa: E402

# Raw-SQL fixtures must hit the DB the module under test actually bound
# (module-level DATABASE_PATH is read at import; when several test modules
# share a process, the first import wins -- so never trust the env here).
DB = notepad.DB_PATH
SAMPLE = ("Pecan Grove condo, buyers John & Jane Doe, 685k, earnest maybe 7k ??, "
          "10 day option $350, signed Nov 10, close Dec 18, fridge + washer stay, "
          "roof credit 5k, 20% down, Lake Travis Title")


import itertools  # noqa: E402

_PHONE_SEQ = itertools.count(100)


def _fresh_phone():
    return f"+1514777{next(_PHONE_SEQ):04d}"


def _mkuser(phone, plan=None, roster=False):
    subscriptions.create_user(phone)
    if plan:
        subscriptions.activate_subscription(phone, "cus_t", "sub_t", plan)
    if roster:
        conn = sqlite3.connect(subscriptions.DB_PATH)
        conn.execute("UPDATE users SET brokerage_id = 7 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()
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


class NotesVersioning(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone())
        self.tx = notepad.create_transaction(self.phone)

    def test_notes_are_append_only_and_verbatim(self):
        messy = "  first draft of my thoughts...\n685k??  "
        n1 = notepad.add_note(self.tx["id"], self.phone, messy)
        n2 = notepad.add_note(self.tx["id"], self.phone, "second version")
        history = notepad.list_notes(self.tx["id"])
        self.assertEqual([n["id"] for n in history], [n2["id"], n1["id"]])
        self.assertEqual(history[1]["raw_text"], messy)  # byte-for-byte
        self.assertEqual(notepad.get_latest_note(self.tx["id"])["raw_text"],
                         "second version")

    def test_ownership_scoping(self):
        other = _mkuser("+15149998888")
        self.assertIsNone(notepad.get_transaction(self.tx["id"], other))
        self.assertIsNone(notepad.add_note(self.tx["id"], other, "intruder"))
        self.assertIsNone(notepad.get_deal_sheet(self.tx["id"], other))
        self.assertFalse(notepad.soft_delete_transaction(self.tx["id"], other))

    def test_soft_delete_then_purge_after_30_days(self):
        notepad.add_note(self.tx["id"], self.phone, "to be purged")
        self.assertTrue(notepad.soft_delete_transaction(self.tx["id"], self.phone))
        self.assertIsNone(notepad.get_transaction(self.tx["id"], self.phone))
        self.assertEqual(notepad.list_transactions(self.phone), [])
        self.assertEqual(notepad.purge_expired_deleted(), 0)  # too fresh
        conn = sqlite3.connect(DB)
        conn.execute("UPDATE transactions SET deleted_at = '2020-01-01T00:00:00' "
                     "WHERE id = ?", (self.tx["id"],))
        conn.commit()
        conn.close()
        self.assertEqual(notepad.purge_expired_deleted(), 1)
        self.assertEqual(notepad.list_notes(self.tx["id"]), [])


class MergeRules(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone())
        self.tx = notepad.create_transaction(self.phone)

    def test_enhance_never_overwrites_user_value(self):
        _apply(self.phone, self.tx["id"], SAMPLE)
        notepad.set_field(self.tx["id"], self.phone, "price", 690000)
        merged = _apply(self.phone, self.tx["id"], SAMPLE)
        sheet = notepad.get_deal_sheet(self.tx["id"], self.phone)
        self.assertEqual(sheet["fields"]["price"]["value"], 690000)
        self.assertEqual(sheet["fields"]["price"]["state"], "found")
        self.assertTrue(any("Sales price" in w for w in merged["warnings"]))

    def test_confirm_never_overwrites_found(self):
        _apply(self.phone, self.tx["id"], SAMPLE)
        notepad.set_field(self.tx["id"], self.phone, "earnest_money_amount",
                          7000, state="found", origin="user")
        result = note_enhancer.enhance_note("earnest $9,000 on this one", "TX", None)
        validated, vw = note_enhancer.validate_proposal(
            "earnest $9,000 on this one", result["fields"], "TX")
        note = notepad.add_note(self.tx["id"], self.phone, "earnest $9,000 on this one")
        notepad.apply_enhancement(self.tx["id"], self.phone, note, result,
                                  validated, validation_warnings=vw)
        sheet = notepad.get_deal_sheet(self.tx["id"], self.phone)
        self.assertEqual(sheet["fields"]["earnest_money_amount"]["value"], 7000)

    def test_profile_defaults_fill_only_missing_slots(self):
        profile = {"title_company": "FirstChoice Title",
                   "default_earnest_pct": 0.01, "default_option_fee": 250}
        _apply(self.phone, self.tx["id"], "just a condo, 500k, nothing else yet",
               profile=profile)
        sheet = notepad.get_deal_sheet(self.tx["id"], self.phone)
        self.assertEqual(sheet["fields"]["title_company"]["origin"], "profile_default")
        self.assertEqual(sheet["fields"]["title_company"]["state"], "confirm")
        self.assertEqual(sheet["fields"]["earnest_pct"]["value"], 0.01)

    def test_audit_row_written(self):
        _apply(self.phone, self.tx["id"], SAMPLE)
        rows = notepad.list_enhancements(self.tx["id"], self.phone)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "regex")
        self.assertEqual(rows[0]["status"], "fallback_regex")

    def test_sheet_to_parsed_only_firm_fields(self):
        _apply(self.phone, self.tx["id"], SAMPLE)
        fields = notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"]
        self.assertEqual(notepad.sheet_to_parsed(fields), {})
        notepad.set_field(self.tx["id"], self.phone, "price", 685000)
        notepad.set_field(self.tx["id"], self.phone, "option_days", 10)
        parsed = notepad.sheet_to_parsed(
            notepad.get_deal_sheet(self.tx["id"], self.phone)["fields"])
        self.assertEqual(parsed, {"price": 685000, "inspection_days": 10})


class AccessMatrix(unittest.TestCase):
    def test_matrix(self):
        self.assertFalse(subscriptions.has_notepad_access("+15140000000"))
        free = _mkuser("+15140000001")
        self.assertFalse(subscriptions.has_notepad_access(free))
        for i, plan in enumerate(("starter", "professional", "brokerage")):
            phone = _mkuser(f"+1514000001{i}", plan=plan)
            self.assertTrue(subscriptions.has_notepad_access(phone), plan)
        roster = _mkuser("+15140000019", roster=True)
        self.assertTrue(subscriptions.has_notepad_access(roster))
        with mock.patch.object(subscriptions, "ADMIN_PHONES", {"+15140000020"}):
            self.assertTrue(subscriptions.has_notepad_access("+15140000020"))
        lapsed = _mkuser("+15140000021", plan="starter")
        subscriptions.deactivate_subscription("sub_t")  # keyed by Stripe sub id
        self.assertFalse(subscriptions.has_notepad_access(lapsed))


class NotepadRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_bad_signature_gets_access_expired(self):
        resp = self.client.get("/notepad?phone=%2B15140000030&expires=1&sig=bad")
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b"Access Expired", resp.data)

    def test_free_user_sees_upsell_and_cannot_create(self):
        phone = _mkuser("+15140000031")
        q = _signed(phone)
        resp = self.client.get("/notepad", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"part of the Agent plan", resp.data)
        resp = self.client.post("/notepad", query_string=q,
                                data={"state": "TX", "title": "Nope"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"part of the Agent plan", resp.data)
        self.assertEqual(notepad.list_transactions(phone), [])

    def test_starter_full_flow(self):
        phone = _mkuser("+15140000032", plan="starter")
        q = _signed(phone)
        resp = self.client.post("/notepad", query_string=q,
                                data={"state": "TX", "title": ""})
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["Location"]
        for token in ("phone=", "expires=", "sig="):
            self.assertIn(token, loc)
        tx_path = urlparse(loc).path
        tx_id = int(tx_path.rsplit("/", 1)[1])

        resp = self.client.get(loc)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Deal Sheet", resp.data)
        self.assertIn(b"Not in your notes", resp.data)

        resp = self.client.post(f"/notepad/{tx_id}/notes", query_string=q,
                                json={"raw_text": SAMPLE})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = self.client.post(f"/notepad/{tx_id}/enhance", query_string=q, json={})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["status"], "fallback_regex")
        self.assertEqual(payload["fields"]["price"]["value"], 685000)
        self.assertEqual(payload["fields"]["price"]["state"], "confirm")

        # Nothing reaches a draft until the user firms a field.
        sheet = notepad.get_deal_sheet(tx_id, phone)
        self.assertEqual(notepad.sheet_to_parsed(sheet["fields"]), {})

        resp = self.client.post(f"/notepad/{tx_id}/fields", query_string=q,
                                json={"key": "price", "action": "accept"})
        self.assertEqual(resp.get_json()["fields"]["price"]["state"], "found")
        resp = self.client.post(f"/notepad/{tx_id}/fields", query_string=q,
                                json={"key": "price", "value": "700000"})
        self.assertEqual(resp.get_json()["fields"]["price"]["value"], 700000)
        resp = self.client.post(f"/notepad/{tx_id}/fields", query_string=q,
                                json={"key": "price", "action": "clear"})
        self.assertNotIn("price", resp.get_json()["fields"])

        # The dashboard links into the notepad with the signed trio intact.
        resp = self.client.get("/dashboard", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"/notepad?phone=", resp.data)
        self.assertIn(b"sig=", resp.data)

        # Delete is soft: the detail page 404s afterwards.
        resp = self.client.post(f"/notepad/{tx_id}/delete", query_string=q)
        self.assertEqual(resp.status_code, 302)
        resp = self.client.get(f"/notepad/{tx_id}", query_string=q)
        self.assertEqual(resp.status_code, 404)

    def test_cross_user_detail_is_404(self):
        owner = _mkuser("+15140000033", plan="starter")
        tx = notepad.create_transaction(owner)
        stranger = _mkuser("+15140000034", plan="starter")
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=_signed(stranger))
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(f"/notepad/{tx['id']}/enhance",
                                query_string=_signed(stranger), json={})
        self.assertEqual(resp.status_code, 404)

    def test_free_user_enhance_is_403_json(self):
        owner = _mkuser("+15140000035", plan="starter")
        tx = notepad.create_transaction(owner)
        # Simulate a lapsed plan: the transaction exists, access is gone.
        subscriptions.deactivate_subscription("sub_t")  # keyed by Stripe sub id
        resp = self.client.post(f"/notepad/{tx['id']}/enhance",
                                query_string=_signed(owner), json={})
        self.assertEqual(resp.status_code, 403)

    def test_detail_page_has_deal_sheet_download(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        tx = notepad.create_transaction(phone, title="123 Main St")
        notepad.add_note(tx["id"], phone,
                         "Buyers liked the place, earnest maybe 7k, close Dec 18")
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        # The button and its client-side builder ride along on the page,
        # fed by the already-embedded window.NP config (txId included).
        self.assertIn(b"Download Deal Sheet", resp.data)
        self.assertIn(b"npDownload", resp.data)
        self.assertIn(b"deal-sheet-", resp.data)
        self.assertIn(b'"txId": ' + str(tx["id"]).encode(), resp.data)
        self.assertIn(
            b"Saved in your TxtAnOffer dashboard. "
            b"Contract files are checked, then deleted.", resp.data)

    def test_detail_page_note_cannot_break_out_of_script(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        tx = notepad.create_transaction(phone, title="Payload deal")
        payload = "</script><script>alert(1)</script>"
        notepad.add_note(tx["id"], phone, "Buyer wants " + payload + " in writing")
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Download Deal Sheet", resp.data)
        # The note text is never embedded raw in the download path (the
        # builder reads the textarea at click time); everywhere the stored
        # note is rendered (textarea, verbatim history) it is HTML-escaped,
        # so the payload sequence must not appear verbatim anywhere.
        self.assertNotIn(payload.encode(), resp.data)
        self.assertIn(b"&lt;/script&gt;", resp.data)


class PopWidget(unittest.TestCase):
    """The floating pop-up notepad: rendered only for members with
    notepad access, on the dashboard and on both notepad pages, and its
    save flow rides the existing form endpoints -- create answers with a
    redirect to the new deal's signed detail URL (how the panel learns
    the id), and a saved note answers with a redirect to the detail
    page (how the panel knows it landed)."""

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_dashboard_widget_for_paid_user(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        notepad.create_transaction(phone, title="742 Evergreen Terrace")
        resp = self.client.get("/dashboard", query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"npopPill", resp.data)
        self.assertIn(b"npopPanel", resp.data)
        self.assertIn(b"window.NPOP", resp.data)
        # The picker carries the member's deals, escaped like the list page.
        self.assertIn(b"742 Evergreen Terrace", resp.data)
        self.assertIn("＋ New deal…".encode(), resp.data)

    def test_dashboard_widget_hidden_for_free_user(self):
        phone = _mkuser(_fresh_phone())
        resp = self.client.get("/dashboard", query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"npopPill", resp.data)
        self.assertNotIn(b"window.NPOP", resp.data)

    def test_list_and_detail_pages_have_widget(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        tx = notepad.create_transaction(phone, title="1 Picker Way")
        resp = self.client.get("/notepad", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"npopPill", resp.data)
        self.assertIn(b"1 Picker Way", resp.data)
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"npopPill", resp.data)
        self.assertIn(b"npopPanel", resp.data)

    def test_list_page_widget_hidden_for_free_user(self):
        phone = _mkuser(_fresh_phone())
        resp = self.client.get("/notepad", query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"npopPill", resp.data)

    def test_widget_js_posts_forms_with_signed_trio(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        resp = self.client.get("/notepad", query_string=_signed(phone))
        data = resp.data
        # Same form encoding the pages' own forms use, trio in the URLs.
        self.assertIn(b"URLSearchParams", data)
        self.assertIn(b"'/notepad/' + id + '/notes?'", data)
        self.assertIn(b'"createUrl": "/notepad?phone=', data)
        self.assertIn(b'"trio": "phone=', data)
        self.assertIn(b"&expires=", data)
        self.assertIn(b"&sig=", data)
        # The success signals the JS keys on: the redirect targets.
        self.assertIn(b"finalPath(resp) === '/notepad/' + id", data)
        self.assertIn(b"'/notepad/' + id + '?'", data)  # View Deal Sheet URL

    def test_panel_create_then_note_flow(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        # Step 1, exactly the panel's create POST (form fields title/state):
        # the app answers 302 to the new deal's signed detail URL.
        resp = self.client.post("/notepad", query_string=q,
                                data={"title": "Panel Deal", "state": "TX"})
        self.assertEqual(resp.status_code, 302)
        loc = resp.headers["Location"]
        tx_id = int(urlparse(loc).path.rsplit("/", 1)[1])
        # Step 2, the panel's note POST (form field raw_text): success is
        # the redirect landing on the detail page, note stored verbatim.
        note_text = "panel note — verbatim, earnest maybe 7k??"
        resp = self.client.post(f"/notepad/{tx_id}/notes", query_string=q,
                                data={"raw_text": note_text},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.history), 1)
        self.assertEqual(resp.history[0].status_code, 302)
        self.assertEqual(urlparse(resp.history[0].headers["Location"]).path,
                         f"/notepad/{tx_id}")
        self.assertEqual(notepad.get_latest_note(tx_id)["raw_text"], note_text)
        # Step 3: a second note appends to the same deal (panel keeps the
        # picker on the saved deal).
        resp = self.client.post(f"/notepad/{tx_id}/notes", query_string=q,
                                data={"raw_text": "second panel note"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(notepad.get_latest_note(tx_id)["raw_text"],
                         "second panel note")
        self.assertEqual(len(notepad.list_notes(tx_id)), 2)

    def test_panel_failure_shape_when_access_lapses(self):
        # The form-post failure the JS must recognise: no redirect at all,
        # just the upsell page with a 200 -- so finalPath never equals the
        # detail path and the panel keeps the typed note.
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        tx = notepad.create_transaction(phone)
        subscriptions.deactivate_subscription("sub_t")  # keyed by Stripe sub id
        resp = self.client.post(f"/notepad/{tx['id']}/notes", query_string=q,
                                data={"raw_text": "must not be stored"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"part of the Agent plan", resp.data)
        self.assertIsNone(notepad.get_latest_note(tx["id"]))


if __name__ == "__main__":
    unittest.main()
