"""Deal Notepad Phases 2+3 (2026-11-11): action items, reminders,
pinned TC Checks, create-draft, and the live detection checklist.

The invariants under test: task generation dedupes by key and never
resurrects or un-does a done item; reminders come only from firm sheet
dates (never invented); a pinned check stores only its result (the
uploaded bytes are deleted); a draft refuses while any contract field
is still Confirm and otherwise goes through the SMS flow's own
generation path (fill_offer_pdf / record_offer, mocked at the module
boundary like the rest of the suite).

    python3 -m unittest tests.test_notepad_phases23
"""
import io
import itertools
import os
import shutil
import sqlite3
import subprocess
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

DB = notepad.DB_PATH
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
SAMPLE = ("Pecan Grove condo, buyers John & Jane Doe, 685k, earnest maybe 7k ??, "
          "10 day option $350, signed Nov 10, close Dec 18, fridge + washer stay, "
          "roof credit 5k, 20% down, Lake Travis Title")

_PHONE_SEQ = itertools.count(500)


def _fresh_phone():
    return f"+1514888{next(_PHONE_SEQ):04d}"


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


def _keys(tasks):
    return {t["key"]: t for t in tasks}


class TaskGeneration(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)
        _apply(self.phone, self.tx["id"], SAMPLE)

    def test_confirm_items_generated(self):
        tasks = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        self.assertIn("confirm:earnest_money_amount", tasks)
        self.assertEqual(tasks["confirm:earnest_money_amount"]["label"],
                         "Confirm Earnest money: $7,000")
        self.assertIn("confirm:price", tasks)
        self.assertIn("run_tc_check", tasks)
        # Earnest is only Confirm, so no delivery item yet; price is only
        # Confirm, so no send-the-draft item yet.
        self.assertNotIn("earnest_delivery", tasks)
        self.assertNotIn("send_draft", tasks)

    def test_dedupe_on_regeneration(self):
        first = notepad.generate_tasks(self.tx["id"], self.phone)
        second = notepad.generate_tasks(self.tx["id"], self.phone)
        self.assertEqual([t["id"] for t in first], [t["id"] for t in second])

    def test_done_persists_and_survives_regeneration(self):
        tasks = notepad.generate_tasks(self.tx["id"], self.phone)
        item = _keys(tasks)["confirm:price"]
        self.assertTrue(notepad.set_task_done(item["id"], self.tx["id"], self.phone, True))
        again = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        self.assertEqual(again["confirm:price"]["id"], item["id"])
        self.assertEqual(again["confirm:price"]["done"], 1)
        self.assertTrue(again["confirm:price"]["done_at"])

    def test_done_confirm_item_survives_firming(self):
        tasks = notepad.generate_tasks(self.tx["id"], self.phone)
        item = _keys(tasks)["confirm:earnest_money_amount"]
        notepad.set_task_done(item["id"], self.tx["id"], self.phone, True)
        notepad.set_field(self.tx["id"], self.phone, "earnest_money_amount",
                          7000, state="found", origin="user")
        again = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        # Done items are history: never pruned, never un-done.
        self.assertIn("confirm:earnest_money_amount", again)
        self.assertEqual(again["confirm:earnest_money_amount"]["done"], 1)
        self.assertIn("earnest_delivery", again)

    def test_undone_confirm_item_pruned_when_firmed(self):
        notepad.generate_tasks(self.tx["id"], self.phone)
        notepad.set_field(self.tx["id"], self.phone, "earnest_money_amount",
                          7000, state="found", origin="user")
        again = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        self.assertNotIn("confirm:earnest_money_amount", again)
        self.assertIn("earnest_delivery", again)
        self.assertIn("$7,000", again["earnest_delivery"]["label"])

    def test_send_draft_needs_found_price_and_buyer(self):
        notepad.set_field(self.tx["id"], self.phone, "price", 685000,
                          state="found", origin="user")
        tasks = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        self.assertNotIn("send_draft", tasks)
        notepad.set_field(self.tx["id"], self.phone, "buyer_name", "John & Jane Doe",
                          state="found", origin="user")
        tasks = _keys(notepad.generate_tasks(self.tx["id"], self.phone))
        self.assertIn("send_draft", tasks)

    def test_survey_and_hoa_mentions(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _apply(phone, tx["id"], SAMPLE + " Old survey from 2019 is in the file."
                                         " HOA dues are $250 a month.")
        tasks = _keys(notepad.generate_tasks(tx["id"], phone))
        self.assertIn("survey", tasks)
        self.assertIn("hoa_docs", tasks)

    def test_custom_add_and_delete(self):
        tid = notepad.add_task(self.tx["id"], self.phone, "Call the lender")
        tasks = _keys(notepad.list_tasks(self.tx["id"], self.phone))
        self.assertTrue(any(k.startswith("custom:") for k in tasks))
        mine = [t for t in tasks.values() if t["id"] == tid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["label"], "Call the lender")
        # Regeneration never prunes custom items.
        notepad.generate_tasks(self.tx["id"], self.phone)
        self.assertIn(tid, [t["id"] for t in notepad.list_tasks(self.tx["id"], self.phone)])
        self.assertTrue(notepad.delete_task(tid, self.tx["id"], self.phone))
        self.assertNotIn(tid, [t["id"] for t in notepad.list_tasks(self.tx["id"], self.phone)])

    def test_model_scoping(self):
        other = _mkuser(_fresh_phone(), plan="starter")
        self.assertEqual(notepad.list_tasks(self.tx["id"], other), [])
        self.assertFalse(notepad.set_task_done(1, self.tx["id"], other, True))
        self.assertIsNone(notepad.add_task(self.tx["id"], other, "nope"))


class TaskRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _deal_with_tasks(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        resp = self.client.post("/notepad", query_string=q, data={"state": "TX", "title": ""})
        tx_id = int(urlparse(resp.headers["Location"]).path.rsplit("/", 1)[1])
        self.client.post(f"/notepad/{tx_id}/notes", query_string=q, json={"raw_text": SAMPLE})
        resp = self.client.post(f"/notepad/{tx_id}/enhance", query_string=q, json={})
        self.assertTrue(resp.get_json()["ok"])
        return phone, q, tx_id

    def test_enhance_response_carries_tasks(self):
        phone, q, tx_id = self._deal_with_tasks()
        resp = self.client.post(f"/notepad/{tx_id}/enhance", query_string=q, json={})
        keys = {t["key"] for t in resp.get_json()["tasks"]}
        self.assertIn("confirm:earnest_money_amount", keys)

    def test_toggle_add_delete_roundtrip(self):
        phone, q, tx_id = self._deal_with_tasks()
        resp = self.client.post(f"/notepad/{tx_id}/tasks", query_string=q,
                                json={"action": "add", "label": "Order the appraisal"})
        self.assertEqual(resp.status_code, 200)
        tasks = resp.get_json()["tasks"]
        custom = [t for t in tasks if t["label"] == "Order the appraisal"]
        self.assertEqual(len(custom), 1)
        resp = self.client.post(f"/notepad/{tx_id}/tasks", query_string=q,
                                json={"action": "toggle", "id": custom[0]["id"], "done": 1})
        toggled = [t for t in resp.get_json()["tasks"] if t["id"] == custom[0]["id"]]
        self.assertEqual(toggled[0]["done"], 1)
        resp = self.client.post(f"/notepad/{tx_id}/tasks", query_string=q,
                                json={"action": "delete", "id": custom[0]["id"]})
        self.assertNotIn(custom[0]["id"], [t["id"] for t in resp.get_json()["tasks"]])
        self.assertNotIn(custom[0]["id"],
                         [t["id"] for t in notepad.list_tasks(tx_id, phone)])

    def test_cross_user_tasks_404(self):
        phone, q, tx_id = self._deal_with_tasks()
        other_q = _signed(_mkuser(_fresh_phone(), plan="starter"))
        resp = self.client.post(f"/notepad/{tx_id}/tasks", query_string=other_q,
                                json={"action": "add", "label": "intruder"})
        self.assertEqual(resp.status_code, 404)

    def test_free_user_tasks_403(self):
        phone = _mkuser(_fresh_phone())
        tx = notepad.create_transaction(phone)
        q = _signed(phone)
        resp = self.client.post(f"/notepad/{tx['id']}/tasks", query_string=q,
                                json={"action": "add", "label": "x"})
        self.assertEqual(resp.status_code, 403)


class Reminders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _sheet_with_dates(self, phone, tx_id, state="found"):
        today = deadlines.texas_date()
        notepad.set_field(tx_id, phone, "effective_date", today.isoformat(),
                          state=state, origin="user")
        notepad.set_field(tx_id, phone, "option_days", 10,
                          state=state, origin="user")
        closing = today + timedelta(days=30)
        notepad.set_field(tx_id, phone, "closing_date", closing.isoformat(),
                          state=state, origin="user")
        return today, closing

    def test_deadlines_from_firm_dates(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        today, closing = self._sheet_with_dates(phone, tx["id"])
        sheet = notepad.get_deal_sheet(tx["id"], phone)
        rows = notepad.deal_deadlines(tx, sheet["fields"])
        by_kind = {r["kind"]: r for r in rows}
        self.assertEqual(set(by_kind), {"earnest", "option", "closing"})
        self.assertEqual(by_kind["earnest"]["date"],
                         deadlines.earnest_money_deadline(today))
        self.assertEqual(by_kind["option"]["date"],
                         deadlines.option_end_date(today, 10))
        self.assertEqual(by_kind["option"]["note"], "5:00 PM")
        self.assertEqual(by_kind["closing"]["date"], closing)
        self.assertEqual(notepad.relative_label(closing, today), "in 30 days")
        self.assertEqual(notepad.relative_label(today, today), "today")

    def test_confirm_dates_produce_no_rows(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        self._sheet_with_dates(phone, tx["id"], state="confirm")
        sheet = notepad.get_deal_sheet(tx["id"], phone)
        self.assertEqual(notepad.deal_deadlines(tx, sheet["fields"]), [])

    def test_missing_anchors_render_no_fabricated_rows(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        sheet = notepad.get_deal_sheet(tx["id"], phone)
        self.assertEqual(notepad.deal_deadlines(tx, sheet["fields"]), [])
        q = _signed(phone)
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No dates on the sheet yet", resp.data)

    def test_detail_page_renders_closing_row(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        today, closing = self._sheet_with_dates(phone, tx["id"])
        q = _signed(phone)
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Closing", resp.data)
        self.assertIn(b"in 30 days", resp.data)
        self.assertIn(closing.strftime("%a, %b %d, %Y").encode(), resp.data)
        self.assertIn(b"Earnest money due", resp.data)
        self.assertIn(b"Option period ends", resp.data)


class PinnedChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _deal(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        return phone, _signed(phone), tx["id"]

    def _upload(self, q, tx_id, payload, name="contract.pdf"):
        return self.client.post(
            f"/notepad/{tx_id}/check", query_string=q,
            data={"file": (io.BytesIO(payload), name)},
            content_type="multipart/form-data")

    def test_check_stores_result_and_deletes_upload(self):
        phone, q, tx_id = self._deal()
        seen = {}

        def fake_check(paths):
            seen["path"] = paths[0]
            return {"recognized": True, "issues": [
                {"severity": "warning", "message": "Review the survey paragraph.", "key": "survey"},
                {"severity": "blocker", "message": "Earnest money amount is blank.", "key": "earnest"},
                {"severity": "blocker", "message": "Option fee does not match the receipt.", "key": "option_fee"},
            ]}

        with open(os.path.join(FIXTURES, "trec_20-19_planted_errors.pdf"), "rb") as fh:
            payload = fh.read()
        with mock.patch.object(app_module, "check_tc_file", fake_check):
            resp = self._upload(q, tx_id, payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        check = body["check"]
        self.assertTrue(check["recognized"])
        self.assertEqual(check["must_fix_count"], 2)
        self.assertEqual(check["review_count"], 1)
        self.assertEqual(check["form_label"], "TREC 20-19")
        # The uploaded bytes are gone; only the result was stored.
        self.assertFalse(os.path.exists(seen["path"]))
        stored = notepad.list_checks(tx_id, phone)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["id"], check["id"])
        # Detail page renders the summary, must-fix first.
        resp = self.client.get(f"/notepad/{tx_id}", query_string=q)
        self.assertIn(b"2 must fix", resp.data)
        self.assertLess(resp.data.index(b"Earnest money amount is blank."),
                        resp.data.index(b"Review the survey paragraph."))

    def test_unrecognized_upload_stored_honestly(self):
        phone, q, tx_id = self._deal()

        def fake_check(paths):
            return {"recognized": False, "complete": False, "issues": [
                {"severity": "info", "message": "Not a form we can check.", "key": "unreadable"}]}

        with mock.patch.object(app_module, "check_tc_file", fake_check):
            resp = self._upload(q, tx_id, b"%PDF-1.4 fake but pdf-shaped")
        self.assertEqual(resp.status_code, 200)
        check = resp.get_json()["check"]
        self.assertFalse(check["recognized"])
        self.assertEqual(check["must_fix_count"], 0)
        stored = notepad.list_checks(tx_id, phone)
        self.assertEqual(len(stored), 1)
        self.assertFalse(stored[0]["recognized"])
        resp = self.client.get(f"/notepad/{tx_id}", query_string=q)
        self.assertIn(b"could not be read as a TREC or Colorado contract", resp.data)

    def test_non_pdf_rejected_and_nothing_stored(self):
        phone, q, tx_id = self._deal()
        resp = self._upload(q, tx_id, b"just some text", name="notes.txt")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(notepad.list_checks(tx_id, phone), [])

    def test_real_fixture_is_recognized(self):
        phone, q, tx_id = self._deal()
        with open(os.path.join(FIXTURES, "trec_20-19_planted_errors.pdf"), "rb") as fh:
            payload = fh.read()
        resp = self._upload(q, tx_id, payload)
        self.assertEqual(resp.status_code, 200)
        check = resp.get_json()["check"]
        self.assertTrue(check["recognized"])
        self.assertGreaterEqual(check["must_fix_count"], 1)
        self.assertEqual(len(notepad.list_checks(tx_id, phone)), 1)

    def test_cross_user_check_404(self):
        phone, q, tx_id = self._deal()
        other_q = _signed(_mkuser(_fresh_phone(), plan="starter"))
        resp = self._upload(other_q, tx_id, b"%PDF-1.4 x")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(notepad.list_checks(tx_id, phone), [])


def _found_sheet(phone, tx_id, state="TX"):
    """A fully-firmed sheet: every contract field set as found."""
    today = deadlines.texas_date()
    values = {
        "address": "123 Main St, Austin, TX 78704" if state == "TX"
                   else "123 Main St, Denver, CO 80202",
        "city": "Austin" if state == "TX" else "Denver",
        "county": "Travis" if state == "TX" else "Denver",
        "buyer_name": "John Doe",
        "seller_name": "Jane Roe",
        "price": 500000,
        "down_payment_pct": 0.2,
        "financing_type": "conventional",
        "earnest_money_amount": 5000,
        "effective_date": today.isoformat(),
        "close_days": 30,
        "closing_date": (today + timedelta(days=30)).isoformat(),
    }
    if state == "TX":
        values["option_fee_amount"] = 250
        values["option_days"] = 10
        values["title_company"] = "Lake Travis Title"
    for key, value in values.items():
        notepad.set_field(tx_id, phone, key, value, state="found", origin="user")


class CreateDraft(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _offer_count(self, phone):
        conn = sqlite3.connect(DB)
        n = conn.execute("SELECT COUNT(*) FROM offers WHERE phone = ?",
                         (phone,)).fetchone()[0]
        conn.close()
        return n

    def test_blocking_confirm_refuses_and_creates_nothing(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _found_sheet(phone, tx["id"])
        notepad.set_field(tx["id"], phone, "earnest_money_amount", 5000,
                          state="confirm", origin="regex")
        q = _signed(phone)
        before = self._offer_count(phone)
        with mock.patch.object(app_module, "fill_offer_pdf") as fill:
            resp = self.client.post(f"/notepad/{tx['id']}/draft", query_string=q, json={})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("Earnest money", body["blocking"])
        fill.assert_not_called()
        self.assertEqual(self._offer_count(phone), before)
        after = notepad.get_transaction(tx["id"], phone)
        self.assertEqual(after["offer_filename"], "")

    def test_missing_down_payment_refused(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _found_sheet(phone, tx["id"])
        notepad.set_field(tx["id"], phone, "down_payment_pct", None)
        q = _signed(phone)
        with mock.patch.object(app_module, "fill_offer_pdf") as fill:
            resp = self.client.post(f"/notepad/{tx['id']}/draft", query_string=q, json={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Down payment", resp.get_json()["missing"])
        fill.assert_not_called()

    def test_draft_success_records_offer_and_links_pdf(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _found_sheet(phone, tx["id"])
        q = _signed(phone)
        captured = {}

        def fake_fill(parsed, source):
            captured["parsed"] = parsed
            path = os.path.join(os.environ["OFFER_OUTPUT_DIR"], "offer_test.pdf")
            with open(path, "wb") as fh:
                fh.write(b"%PDF-1.4 draft")
            return path

        with mock.patch.object(app_module, "fill_offer_pdf", fake_fill), \
                mock.patch.object(app_module, "check_tc_file",
                                  lambda paths: {"recognized": True, "issues": []}):
            resp = self.client.post(f"/notepad/{tx['id']}/draft", query_string=q, json={})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["filename"], "offer_test.pdf")
        self.assertIn("offer_test.pdf", body["pdf_url"])
        parsed = captured["parsed"]
        self.assertEqual(parsed["price"], 500000)
        self.assertEqual(parsed["down_payment_amount"], 100000)
        self.assertEqual(parsed["loan_amount"], 400000)
        self.assertEqual(parsed["earnest_money"], 5000)
        self.assertEqual(parsed["option_fee"], 250)
        self.assertEqual(parsed["inspection_days"], 10)
        self.assertEqual(parsed["close_days"], 30)
        self.assertEqual(self._offer_count(phone), 1)
        after = notepad.get_transaction(tx["id"], phone)
        self.assertEqual(after["offer_filename"], "offer_test.pdf")
        self.assertEqual(after["status"], "drafted")
        # The detail page now shows the draft on file.
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertIn(b"Draft on file", resp.data)
        self.assertIn(b"offer_test.pdf", resp.data)

    def test_co_draft_uses_cbs1_enrichment(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, state="CO")
        _found_sheet(phone, tx["id"], state="CO")
        q = _signed(phone)
        captured = {}

        def fake_fill(parsed, source):
            captured["parsed"] = parsed
            path = os.path.join(os.environ["OFFER_OUTPUT_DIR"], "offer_co.pdf")
            with open(path, "wb") as fh:
                fh.write(b"%PDF-1.4 draft")
            return path

        with mock.patch.object(app_module, "fill_offer_pdf", fake_fill), \
                mock.patch.object(app_module, "check_tc_file",
                                  lambda paths: {"recognized": True, "issues": []}):
            resp = self.client.post(f"/notepad/{tx['id']}/draft", query_string=q, json={})
        self.assertEqual(resp.status_code, 200)
        parsed = captured["parsed"]
        self.assertEqual(parsed["state"], "CO")
        self.assertEqual(parsed["option_fee"], 0)
        self.assertEqual(parsed["city"], "Denver")
        self.assertTrue(parsed["co_closing"])
        # The firm sheet earnest wins over the profile default.
        self.assertEqual(parsed["earnest_money"], 5000)

    def test_cross_user_draft_404(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        _found_sheet(phone, tx["id"])
        other_q = _signed(_mkuser(_fresh_phone(), plan="starter"))
        resp = self.client.post(f"/notepad/{tx['id']}/draft", query_string=other_q, json={})
        self.assertEqual(resp.status_code, 404)


class DetectionChecklist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def test_markup_present_on_detail_page(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        q = _signed(phone)
        resp = self.client.get(f"/notepad/{tx['id']}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="npDetect"', resp.data)
        self.assertIn(b"In your note so far", resp.data)
        for name in (b"address", b"price", b"earnest", b"option",
                     b"closing", b"financing", b"credit"):
            self.assertIn(b'data-detect="' + name + b'"', resp.data)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_notepad_js_syntax(self):
        path = os.path.join(_TMP, "np_js.js")
        with open(path, "w") as fh:
            fh.write(app_module._NP_JS)
        proc = subprocess.run(["node", "--check", path],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_detection_and_phase23_js_present(self):
        js = app_module._NP_JS
        for token in ("NP_DETECT", "npDetect", "renderTasks", "npTaskToggle",
                      "npTaskAdd", "npTaskDelete", "renderCheck",
                      "npCheckSubmit", "npDraft"):
            self.assertIn(token, js)


if __name__ == "__main__":
    unittest.main()
