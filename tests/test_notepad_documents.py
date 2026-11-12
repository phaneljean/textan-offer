"""Deal Notepad Document Checklist (2026-11-11): per-deal paperwork
status tracking -- Missing / Requested / Received / Reviewed.

The invariants under test: the standard set seeds lazily and
idempotently from the transaction's state (TX carries the T-47
affidavit, CO does not); a status change persists and moves
updated_at; anything outside the four-value enum is rejected; seeded
rows cannot be deleted while custom rows can; and every read and
write is scoped by owner phone -- a transaction id alone never
grants access to another agent's checklist.

    python3 -m unittest tests.test_notepad_documents
"""
import itertools
import os
import shutil
import sqlite3
import subprocess
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
import subscriptions  # noqa: E402

_PHONE_SEQ = itertools.count(900)


def _fresh_phone():
    return f"+1514777{next(_PHONE_SEQ):04d}"


def _mkuser(phone, plan=None):
    subscriptions.create_user(phone)
    if plan:
        subscriptions.activate_subscription(phone, "cus_t", "sub_t", plan)
    return phone


def _signed(phone):
    url = app_module.sign_dashboard_url(phone, "http://localhost")
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


TX_LABELS = ["Purchase agreement", "Seller's disclosure", "HOA documents",
             "Survey", "T-47 affidavit", "Addenda", "Title commitment"]
CO_LABELS = ["Purchase agreement", "Seller's property disclosure",
             "HOA documents", "Survey / ILC", "Addenda", "Title commitment"]


class DocumentSeeding(unittest.TestCase):
    def test_tx_defaults_seed_all_missing(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        docs = notepad.ensure_documents(tx)
        self.assertEqual([d["label"] for d in docs], TX_LABELS)
        self.assertTrue(all(d["status"] == "missing" for d in docs))
        self.assertTrue(all(d["is_custom"] == 0 for d in docs))

    def test_co_defaults_have_no_t47(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, state="CO")
        docs = notepad.ensure_documents(tx)
        self.assertEqual([d["label"] for d in docs], CO_LABELS)
        self.assertNotIn("T-47 affidavit", [d["label"] for d in docs])

    def test_seeding_is_idempotent(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        first = notepad.ensure_documents(tx)
        notepad.set_document_status(first[0]["id"], tx["id"], phone, "received")
        second = notepad.ensure_documents(tx)
        self.assertEqual(len(second), len(first))
        self.assertEqual([d["id"] for d in second], [d["id"] for d in first])
        # A re-view never resets a status the user already set.
        self.assertEqual(second[0]["status"], "received")


class DocumentStatus(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)
        self.docs = notepad.ensure_documents(self.tx)

    def _row(self, doc_id):
        rows = [d for d in notepad.list_documents(self.tx["id"], self.phone)
                if d["id"] == doc_id]
        return rows[0] if rows else None

    def test_status_transitions_persist(self):
        doc = self.docs[0]
        for status in ("requested", "received", "reviewed", "missing"):
            self.assertTrue(notepad.set_document_status(
                doc["id"], self.tx["id"], self.phone, status))
            self.assertEqual(self._row(doc["id"])["status"], status)

    def test_status_change_bumps_updated_at(self):
        doc = self.docs[0]
        conn = sqlite3.connect(notepad.DB_PATH)
        conn.execute("UPDATE transaction_documents SET updated_at = ?"
                     " WHERE id = ?",
                     ("2000-01-01T00:00:00", doc["id"]))
        conn.commit()
        conn.close()
        self.assertTrue(notepad.set_document_status(
            doc["id"], self.tx["id"], self.phone, "requested"))
        self.assertGreater(self._row(doc["id"])["updated_at"],
                           "2000-01-01T00:00:00")

    def test_invalid_status_rejected(self):
        doc = self.docs[0]
        self.assertFalse(notepad.set_document_status(
            doc["id"], self.tx["id"], self.phone, "bogus"))
        self.assertFalse(notepad.set_document_status(
            doc["id"], self.tx["id"], self.phone, ""))
        self.assertEqual(self._row(doc["id"])["status"], "missing")


class DocumentCustomRows(unittest.TestCase):
    def setUp(self):
        self.phone = _mkuser(_fresh_phone(), plan="starter")
        self.tx = notepad.create_transaction(self.phone)
        self.docs = notepad.ensure_documents(self.tx)

    def test_add_and_delete_custom(self):
        doc_id = notepad.add_document(self.tx["id"], self.phone,
                                      "Earnest money receipt")
        self.assertIsNotNone(doc_id)
        docs = notepad.list_documents(self.tx["id"], self.phone)
        custom = [d for d in docs if d["id"] == doc_id]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["is_custom"], 1)
        self.assertEqual(custom[0]["status"], "missing")
        self.assertTrue(notepad.delete_document(doc_id, self.tx["id"],
                                                self.phone))
        self.assertNotIn(doc_id, [d["id"] for d in
                                  notepad.list_documents(self.tx["id"],
                                                         self.phone)])

    def test_seeded_row_delete_refused(self):
        seeded = self.docs[0]
        self.assertFalse(notepad.delete_document(seeded["id"], self.tx["id"],
                                                 self.phone))
        self.assertIn(seeded["id"], [d["id"] for d in
                                     notepad.list_documents(self.tx["id"],
                                                            self.phone)])


class DocumentScoping(unittest.TestCase):
    def test_other_phone_cannot_read_or_mutate(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        other = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone)
        docs = notepad.ensure_documents(tx)
        custom_id = notepad.add_document(tx["id"], phone, "Wire instructions")
        self.assertEqual(notepad.list_documents(tx["id"], other), [])
        self.assertFalse(notepad.set_document_status(
            docs[0]["id"], tx["id"], other, "received"))
        self.assertFalse(notepad.delete_document(custom_id, tx["id"], other))
        self.assertIsNone(notepad.add_document(tx["id"], other, "intruder"))
        # The owner's rows are untouched by all of the above.
        mine = notepad.list_documents(tx["id"], phone)
        self.assertEqual(len(mine), len(docs) + 1)
        self.assertTrue(all(d["status"] == "missing" for d in mine))


class DocumentRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _deal(self, state="TX"):
        phone = _mkuser(_fresh_phone(), plan="starter")
        q = _signed(phone)
        resp = self.client.post("/notepad", query_string=q,
                                data={"state": state, "title": ""})
        tx_id = int(urlparse(resp.headers["Location"]).path.rsplit("/", 1)[1])
        return phone, q, tx_id

    def test_detail_page_seeds_and_renders_checklist(self):
        phone, q, tx_id = self._deal()
        resp = self.client.get(f"/notepad/{tx_id}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Documents", resp.data)
        self.assertIn(b"T-47 affidavit", resp.data)
        self.assertIn(b"0 of 7 received or reviewed", resp.data)
        # The view itself performed the lazy seeding.
        self.assertEqual(len(notepad.list_documents(tx_id, phone)), 7)

    def test_detail_page_renders_cycle_control(self):
        # 2026-11-12: the four per-status pills became ONE
        # click-to-cycle control per row (missing -> requested ->
        # received -> reviewed -> missing), still persisted through
        # the same set_status route (see the roundtrip test above).
        phone, q, tx_id = self._deal()
        resp = self.client.get(f"/notepad/{tx_id}", query_string=q)
        self.assertEqual(resp.status_code, 200)
        docs = notepad.list_documents(tx_id, phone)
        first = docs[0]
        self.assertIn(b'class="docstat doccycle st-missing"', resp.data)
        self.assertIn(
            f"npDocCycle({first['id']}, 'missing')".encode(), resp.data)
        self.assertNotIn(b"docpill", resp.data)

    def test_set_status_add_delete_roundtrip(self):
        phone, q, tx_id = self._deal()
        self.client.get(f"/notepad/{tx_id}", query_string=q)
        docs = notepad.list_documents(tx_id, phone)
        resp = self.client.post(f"/notepad/{tx_id}/documents", query_string=q,
                                json={"action": "set_status",
                                      "id": docs[0]["id"],
                                      "status": "received"})
        self.assertEqual(resp.status_code, 200)
        updated = [d for d in resp.get_json()["documents"]
                   if d["id"] == docs[0]["id"]]
        self.assertEqual(updated[0]["status"], "received")
        resp = self.client.post(f"/notepad/{tx_id}/documents", query_string=q,
                                json={"action": "add",
                                      "label": "Earnest money receipt"})
        self.assertEqual(resp.status_code, 200)
        customs = [d for d in resp.get_json()["documents"]
                   if d["label"] == "Earnest money receipt"]
        self.assertEqual(len(customs), 1)
        resp = self.client.post(f"/notepad/{tx_id}/documents", query_string=q,
                                json={"action": "delete",
                                      "id": customs[0]["id"]})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(customs[0]["id"],
                         [d["id"] for d in resp.get_json()["documents"]])

    def test_invalid_status_400(self):
        phone, q, tx_id = self._deal()
        self.client.get(f"/notepad/{tx_id}", query_string=q)
        docs = notepad.list_documents(tx_id, phone)
        resp = self.client.post(f"/notepad/{tx_id}/documents", query_string=q,
                                json={"action": "set_status",
                                      "id": docs[0]["id"], "status": "bogus"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(notepad.list_documents(tx_id, phone)[0]["status"],
                         "missing")

    def test_seeded_delete_refused_via_route(self):
        phone, q, tx_id = self._deal()
        self.client.get(f"/notepad/{tx_id}", query_string=q)
        docs = notepad.list_documents(tx_id, phone)
        resp = self.client.post(f"/notepad/{tx_id}/documents", query_string=q,
                                json={"action": "delete", "id": docs[0]["id"]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn(docs[0]["id"],
                      [d["id"] for d in notepad.list_documents(tx_id, phone)])

    def test_cross_user_documents_404(self):
        phone, q, tx_id = self._deal()
        other_q = _signed(_mkuser(_fresh_phone(), plan="starter"))
        resp = self.client.post(f"/notepad/{tx_id}/documents",
                                query_string=other_q,
                                json={"action": "add", "label": "intruder"})
        self.assertEqual(resp.status_code, 404)

    def test_free_user_documents_403(self):
        phone = _mkuser(_fresh_phone())
        tx = notepad.create_transaction(phone)
        q = _signed(phone)
        resp = self.client.post(f"/notepad/{tx['id']}/documents",
                                query_string=q,
                                json={"action": "add", "label": "x"})
        self.assertEqual(resp.status_code, 403)

    def test_documents_js_present(self):
        js = app_module._NP_JS
        for token in ("renderDocs", "npDocStatus", "npDocAdd", "npDocDelete",
                      "npDocCycle", "DOC_CYCLE", "documentsUrl"):
            self.assertIn(token, js)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_notepad_js_syntax(self):
        path = os.path.join(_TMP, "np_js.js")
        with open(path, "w") as fh:
            fh.write(app_module._NP_JS)
        proc = subprocess.run(["node", "--check", path],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
