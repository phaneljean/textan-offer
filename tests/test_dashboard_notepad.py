"""Dashboard Notepad hero (2026-11-11): the milestone card is gone from
the dashboard and the Deal Notepad leads the page instead, in the prime
spot under the greeting, before the stats. With deals, the hero carries
the recent deals with their confirm badges; without deals, a paper
preview of a messy note becoming a Deal Sheet. The /wins share page is
untouched. A notepad failure must degrade the hero, never the page.

    python3 -m unittest tests.test_dashboard_notepad
"""
import os
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

SAMPLE = ("Pecan Grove condo, buyers John & Jane Doe, 685k, earnest maybe 7k ??, "
          "10 day option $350, signed Nov 10, close Dec 18, fridge + washer stay, "
          "roof credit 5k, 20% down, Lake Travis Title")

import itertools  # noqa: E402

_PHONE_SEQ = itertools.count(500)


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


def _apply(phone, tx_id, note_text):
    note = notepad.add_note(tx_id, phone, note_text)
    result = note_enhancer.enhance_note(note_text, "TX", None)
    validated, vw = note_enhancer.validate_proposal(note_text, result["fields"], "TX")
    return notepad.apply_enhancement(tx_id, phone, note, result, validated,
                                     validation_warnings=vw, profile=None)


def _hero_slice(html):
    """The hero card's markup: from the np-hero element up to the stats.
    Anchored on the element itself so the .np-hero CSS rules in the
    page's <style> block don't leak into the slice."""
    start = html.index(b'<div class="np-hero">')
    end = html.index(b"Total offers")
    assert start < end
    return html[start:end]


class DashboardHero(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def _dashboard(self, phone):
        resp = self.client.get("/dashboard", query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        return resp.data

    def test_no_milestone_markup_anywhere(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        html = self._dashboard(phone)
        self.assertNotIn(b"milestone", html)
        self.assertNotIn(b"Congrats on", html)
        self.assertNotIn(b"Share your milestone", html)
        self.assertNotIn(b"first accepted offer will show up here", html)

    def test_hero_leads_before_stats_with_signed_link(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        notepad.create_transaction(phone, title="1 Hero Way")
        html = self._dashboard(phone)
        hero = _hero_slice(html)  # asserts hero precedes the stats block
        self.assertIn(b"np-hero", hero)
        self.assertIn(b"DEAL NOTEPAD", hero)
        self.assertIn(b"The notepad that sits on your desk.", hero)
        self.assertIn(b"Open Notepad", hero)
        self.assertIn(b"/notepad?phone=", hero)
        self.assertIn(b"sig=", hero)
        # Exactly one hero on the page.
        self.assertEqual(html.count(b'class="np-hero"'), 1)

    def test_hero_lists_deal_with_confirm_badge(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        tx = notepad.create_transaction(phone, title="742 Evergreen Terrace")
        _apply(phone, tx["id"], SAMPLE)
        self.assertGreater(notepad.list_transactions(phone)[0]["confirm_count"], 0)
        html = self._dashboard(phone)
        hero = _hero_slice(html)
        self.assertIn(b"742 Evergreen Terrace", hero)
        self.assertIn(b"to confirm", hero)
        self.assertIn(b"active deal", hero)
        self.assertIn(b"waiting on confirm", hero)
        self.assertIn(b"All transactions", hero)
        # The row links to the signed deal detail.
        self.assertIn(f"/notepad/{tx['id']}?phone=".encode(), hero)
        # No paper preview when the member has deals.
        self.assertNotIn(b"np-paper", hero)

    def test_hero_empty_state_paper_preview(self):
        phone = _mkuser(_fresh_phone())
        html = self._dashboard(phone)
        hero = _hero_slice(html)
        self.assertIn(b"np-paper", hero)
        self.assertIn(b"123 Main St, Austin", hero)
        self.assertIn(b"Deal Sheet", hero)
        self.assertIn(b"Confirm: earnest", hero)
        self.assertIn(b"Start your first deal", hero)
        self.assertIn(b"/notepad?phone=", hero)

    def test_hero_degrades_when_notepad_fails(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        with mock.patch.object(notepad, "list_transactions",
                               side_effect=Exception("boom")):
            resp = self.client.get("/dashboard", query_string=_signed(phone))
        self.assertEqual(resp.status_code, 200)
        hero = _hero_slice(resp.data)
        self.assertIn(b"The notepad that sits on your desk.", hero)
        self.assertIn(b"Open Notepad", hero)

    def test_wins_route_still_responds(self):
        phone = _mkuser(_fresh_phone(), plan="starter")
        url = app_module.sign_wins_url(phone, "http://localhost")
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        resp = self.client.get("/wins", query_string=q)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"TxtAnOffer", resp.data)


if __name__ == "__main__":
    unittest.main()
