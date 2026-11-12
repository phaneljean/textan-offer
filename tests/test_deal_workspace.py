"""Deal workspace (2026-11-11): the deal detail page reorganized into
a SaaS-style overview in TxtAnOffer's own design language -- a deal
header (firmed address as title, closing countdown, Run TC Check), a
workflow spine, a read-only Deal Summary, and a deterministic Deal
health card -- over the unchanged working cards. The invariants under
test: the header never invents (Confirm address does not become the
title, an unfirmed closing date reads "Not set yet"), the summary
mirrors the Deal Sheet's three states honestly, the health percentage
is the documented average of its three components computed from the
seeded data, the outstanding list names only work that is actually
outstanding, no purple and no sidebar markup arrive from the mock,
and the window.NP payload (the desktop app's sync contract) keeps
exactly its key set.

    python3 -m unittest tests.test_deal_workspace
"""
import json
import os
import re
import tempfile
import unittest
from datetime import timedelta
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
import subscriptions  # noqa: E402

import itertools  # noqa: E402

_PHONE_SEQ = itertools.count(900)

# The canonical Deal Summary fields for Texas, mirrored from the
# documented health formula (the summary shows these; Colorado drops
# the option pair).
CANON_TX = [
    "address", "city", "county", "buyer_name", "seller_name", "price",
    "down_payment_pct", "down_payment_amount", "financing_type",
    "earnest_money_amount", "earnest_pct", "option_fee_amount",
    "option_days", "closing_date", "title_company", "hoa", "inclusions",
    "seller_concessions",
]

NP_KEYS = {"fieldsUrl", "enhanceUrl", "notesUrl", "tasksUrl",
           "documentsUrl", "checkUrl", "draftUrl", "fields", "defs",
           "kinds", "txId"}


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


def _slice(html, start, end):
    i = html.index(start)
    j = html.index(end, i)
    assert i < j
    return html[i:j]


class DealWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _page(self, phone, tx_id):
        resp = self.client.get(f"/notepad/{tx_id}",
                               query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def _header(self, html):
        return _slice(html, b'<div class="ws-header">',
                      b'<div class="ws-spine">')

    # -- Header ------------------------------------------------------
    def test_header_firmed_address_and_closing_countdown(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Original Title")
        notepad.set_field(tx["id"], phone, "address", "123 Main St, Austin")
        notepad.set_field(tx["id"], phone, "city", "Austin")
        closing = deadlines.texas_date() + timedelta(days=36)
        notepad.set_field(tx["id"], phone, "closing_date",
                          closing.isoformat())
        header = self._header(self._page(phone, tx["id"]))
        self.assertIn(b"123 Main St, Austin", header)
        self.assertNotIn(b"Original Title", header)
        self.assertIn(b"Austin, TX", header)
        self.assertIn(closing.strftime("%a, %b %d, %Y").encode(), header)
        self.assertIn(b"in 36 days", header)
        self.assertIn(b"Run TC Check", header)

    def test_header_falls_back_when_address_is_confirm(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Fallback Title Here")
        notepad.set_field(tx["id"], phone, "address",
                          "742 Evergreen Terrace", state="confirm",
                          origin="user")
        header = self._header(self._page(phone, tx["id"]))
        self.assertIn(b"Fallback Title Here", header)
        self.assertNotIn(b"742 Evergreen Terrace", header)
        self.assertIn(b"Not set yet", header)

    # -- Deal Summary --------------------------------------------------
    def test_summary_mirrors_sheet_states(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Summary Deal")
        notepad.set_field(tx["id"], phone, "price", 685000)
        notepad.set_field(tx["id"], phone, "city", "Austin",
                          state="confirm", origin="user")
        # A Captured earnest amount derives the earnest % as
        # Calculated, so the combined earnest line carries calc.
        notepad.set_field(tx["id"], phone, "earnest_money_amount", 7000)
        html = self._page(phone, tx["id"])
        summary = _slice(html, b"<h2>Deal Summary</h2>",
                         b"<h2>Deal health</h2>")
        # Captured renders plainly, no chip on the value.
        self.assertIn(b'Sales price</div><div class="sum-val">$685,000</div>',
                      summary)
        # Confirm carries the amber chip.
        self.assertIn(b'City</div><div class="sum-val">Austin '
                      b'<span class="tag tag-confirm">Confirm</span></div>',
                      summary)
        # Calculated carries the slate chip on the combined line.
        self.assertIn(b"$7,000", summary)
        self.assertIn(b'<span class="tag tag-calc">calc</span>', summary)
        # Missing reads "Not set".
        self.assertIn(b'County</div><div class="sum-val">'
                      b'<span class="sum-missing">Not set</span></div>',
                      summary)
        # The edit anchor points at the Deal Sheet card, which exists.
        self.assertIn(b'href="#wsSheetCard"', summary)
        self.assertIn(b'id="wsSheetCard"', html)

    # -- Deal health ---------------------------------------------------
    def _expected_health(self, phone, tx_id):
        """Recompute the documented formula from the stored data:
        simple average of (terms firmed of canonical, documents
        received-or-reviewed of total, tasks done of total), with an
        empty component counting as complete."""
        fields = notepad.get_deal_sheet(tx_id, phone)["fields"]
        firmed = sum(1 for k in CANON_TX
                     if (fields.get(k) or {}).get("state")
                     in ("found", "calculated")
                     and (fields.get(k) or {}).get("value") not in (None, ""))
        terms = round(100 * firmed / len(CANON_TX))
        docs = notepad.list_documents(tx_id, phone)
        doc_done = sum(1 for d in docs
                       if d["status"] in ("received", "reviewed"))
        docs_pct = round(100 * doc_done / len(docs)) if docs else 100
        tasks = notepad.list_tasks(tx_id, phone)
        tasks_done = sum(1 for t in tasks if t["done"])
        tasks_pct = (round(100 * tasks_done / len(tasks))
                     if tasks else 100)
        return round((terms + docs_pct + tasks_pct) / 3), firmed

    def test_health_matches_formula_on_seeded_deal(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Health Deal")
        notepad.add_note(tx["id"], phone, "123 Main St, 685k, earnest 7k")
        notepad.set_field(tx["id"], phone, "address", "123 Main St, Austin")
        notepad.set_field(tx["id"], phone, "city", "Austin")
        notepad.set_field(tx["id"], phone, "price", 685000)
        notepad.set_field(tx["id"], phone, "earnest_money_amount", 7000,
                          state="confirm", origin="user")
        docs = notepad.ensure_documents(tx)
        for d in docs[:2]:
            notepad.set_document_status(d["id"], tx["id"], phone, "received")
        html = self._page(phone, tx["id"])
        expected, firmed = self._expected_health(phone, tx["id"])
        m = re.search(rb'<span class="health-pct">(\d+)%</span>', html)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), expected)
        self.assertIn(f"Terms firmed</span>".encode(), html)
        self.assertIn(f"{firmed} of {len(CANON_TX)}".encode(), html)

    def test_outstanding_names_only_true_items_on_fresh_deal(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Fresh Deal")
        html = self._page(phone, tx["id"])
        health = _slice(html, b"<h2>Deal health</h2>",
                        b'<div class="np-grid">')
        # Seeded TX documents are all missing; no check, no draft yet.
        self.assertIn(b"documents missing", health)
        self.assertIn(b"No TC Check yet", health)
        self.assertIn(b"No draft yet", health)
        # Nothing is Confirm and no action items exist: neither may
        # appear as outstanding work.
        self.assertNotIn(b"to confirm", health)
        self.assertNotIn(b"action items open", health)

    def test_complete_deal_reads_complete(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Complete Deal")
        notepad.add_note(tx["id"], phone, "a worked deal")
        closing = (deadlines.texas_date() + timedelta(days=30)).isoformat()
        for key, value in (
                ("address", "123 Main St, Austin"), ("city", "Austin"),
                ("county", "Travis"), ("buyer_name", "John Doe"),
                ("seller_name", "Jane Seller"), ("price", 685000),
                ("down_payment_pct", 0.2), ("financing_type", "conventional"),
                ("earnest_money_amount", 7000), ("option_fee_amount", 250),
                ("option_days", 10), ("closing_date", closing),
                ("title_company", "Texas Title"), ("hoa", "HOA docs in"),
                ("inclusions", "Fridge stays"),
                ("seller_concessions", "Roof credit")):
            notepad.set_field(tx["id"], phone, key, value)
        for d in notepad.ensure_documents(tx):
            notepad.set_document_status(d["id"], tx["id"], phone, "received")
        notepad.record_check(tx["id"], phone, "TREC 20-19", True, 0, 0, [])
        notepad.update_transaction(tx["id"], phone,
                                   offer_filename="offer_test.pdf")
        self._page(phone, tx["id"])  # generates action items on view
        for t in notepad.list_tasks(tx["id"], phone):
            notepad.set_task_done(t["id"], tx["id"], phone, True)
        html = self._page(phone, tx["id"])
        health = _slice(html, b"<h2>Deal health</h2>",
                        b'<div class="np-grid">')
        self.assertIn(b'<span class="health-pct">100%</span>', health)
        self.assertIn(b"Complete", health)
        self.assertIn(f"Terms firmed</span>".encode(), health)
        self.assertIn(f"{len(CANON_TX)} of {len(CANON_TX)}".encode(), health)
        self.assertIn(b"This deal is fully worked.", health)
        self.assertNotIn(b"documents missing", health)
        self.assertNotIn(b"action items open", health)

    # -- Spine ---------------------------------------------------------
    def test_spine_marks_confirm_current_and_check_done(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Spine Deal")
        notepad.add_note(tx["id"], phone, "a note")
        notepad.set_field(tx["id"], phone, "price", 685000,
                          state="confirm", origin="user")
        notepad.record_check(tx["id"], phone, "TREC 20-19", True, 1, 0, [])
        html = self._page(phone, tx["id"])
        spine = _slice(html, b'<div class="ws-spine">',
                       b'<div class="ws-grid">')
        self.assertIn(b'ws-step done"><span class="ws-dot">&#10003;</span>Notes',
                      spine)
        self.assertIn(b'ws-step current"><span class="ws-dot">3</span>Confirm',
                      spine)
        self.assertIn(b'ws-step done"><span class="ws-dot">&#10003;</span>TC Check',
                      spine)

    # -- Design guard + sync contract ----------------------------------
    def test_no_purple_no_sidebar_and_np_contract_intact(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="Guard Deal")
        html = self._page(phone, tx["id"])
        low = html.lower()
        for hexval in (b"#7c3aed", b"#6d28d9", b"#8b5cf6", b"#a855f7",
                       b"#9333ea"):
            self.assertNotIn(hexval, low)
        self.assertNotIn(b"sidebar", low)
        self.assertNotIn(b"<aside", low)
        # Anchors and hooks the header button and JS rely on.
        self.assertIn(b'id="wsCheckCard"', html)
        self.assertIn(b'id="npCheckFile"', html)
        self.assertIn(b"npRunCheckFocus", html)
        self.assertIn(b'id="npCheckForm"', html)
        self.assertIn(b'id="sheetRows"', html)
        # window.NP keeps exactly its key set.
        m = re.search(rb"window\.NP=(\{.*?\});\s*</script>", html, re.S)
        self.assertIsNotNone(m)
        cfg = json.loads(m.group(1).decode())
        self.assertEqual(set(cfg.keys()), NP_KEYS)
        self.assertEqual(cfg["txId"], tx["id"])


if __name__ == "__main__":
    unittest.main()
