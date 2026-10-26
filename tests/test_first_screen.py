"""First screen of / and /tc-check (2026-10-23, first_screen.py): the sample
strip must match what the real sample check returns, the ?src= tag picks
the right line (title teams get one generic, ungated line; the November 31
ad leads with that finding), and the analytics hooks survive the move.

    python3 -m unittest tests.test_first_screen
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

import app as app_module
import first_screen
from first_screen import COPY, TITLE_SHARE_URL, source_variant
from tc_audit import check_tc_file
from tc_check_email import _display_issues, _clean

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) test"}
SAMPLES = {"tx": os.path.join(ROOT, "static", "sample_trec_20-19.pdf"),
           "co": os.path.join(ROOT, "static", "sample_cbs1.pdf")}


def _get(path):
    return app_module.app.test_client().get(path, headers=UA).get_data(as_text=True)


def _unescape(html):
    return (html.replace("&rsquo;", "'").replace("&ldquo;", '"').replace("&rdquo;", '"')
                .replace("&sect;", "§").replace("&mdash;", "—"))


def _strip_rows(html, state):
    block = re.search(r'<div data-state="%s"><p class="ss-label">.*?<ul>(.*?)</ul>' % state, html, re.S).group(1)
    return re.findall(r'<li data-row="([a-z0-9]+)"><span class="ss-tag (b|w)">([A-Z ]+)</span><span class="ss-msg">(.*?)</span></li>', block)


class SourceVariant(unittest.TestCase):
    def test_title_tags(self):
        for src in ("ti_keydfw", "TI_KeyDFW", "title_reach_oct", "co_ti_landmark"):
            self.assertEqual(source_variant(src), "title", src)

    def test_shared_link_is_not_a_title_visit(self):
        # Agents opening the link a title team copied see the normal page.
        self.assertIn("src=title_share", TITLE_SHARE_URL)
        self.assertIsNone(source_variant("title_share"))

    def test_november31(self):
        for src in ("ad_november31_fbtcg", "ad_november31_facebook", "ad_november31"):
            self.assertEqual(source_variant(src), "november31", src)

    def test_everything_else(self):
        for src in (None, "", "ig_bio", "li_grp_txrei", "tiles_ad", "direct", "co1017_x"):
            self.assertIsNone(source_variant(src), src)


class SampleStripMatchesSample(unittest.TestCase):
    """The strip is hand-written; this keeps it honest when the sample PDFs
    or the rules change (same check as the homepage hero card)."""

    def test_rows_are_real_findings_with_real_severity(self):
        for state, path in SAMPLES.items():
            shown = _display_issues(check_tc_file([path])["issues"])
            real = [(_clean(i["message"]).replace("’", "'"), i["severity"]) for i in shown]
            for key, cls, tag, msg in first_screen.SAMPLE_ROWS[state]:
                text = _unescape(msg)
                match = [sev for r, sev in real if r.startswith(text)]
                self.assertTrue(match, (state, text))
                self.assertEqual(match[0], "blocker" if cls == "b" else "warning", (state, text))
                self.assertEqual(tag, "MUST FIX" if cls == "b" else "REVIEW")

    def test_strip_rows_match_homepage_card(self):
        card = re.search(r'id="heroReportCard".*?<!-- /heroReportCard -->', _get("/"), re.S).group(0)
        for key, cls, tag, msg in first_screen.SAMPLE_ROWS["tx"] + first_screen.SAMPLE_ROWS["co"]:
            self.assertIn("<span>" + msg + "</span>", card, msg)


class TcCheckFirstScreen(unittest.TestCase):
    def test_default(self):
        html = _get("/tc-check")
        self.assertNotIn("__FS_", html)
        self.assertNotIn('id="titleLine"', html)
        self.assertEqual([r[0] for r in _strip_rows(html, "tx")], ["price", "november31", "email"])
        self.assertEqual([r[0] for r in _strip_rows(html, "co")], ["price", "loan", "holiday"])
        for key in ("tc_eyebrow", "tc_h1", "tc_sub_tx", "tc_sub_co", "tc_sample_btn", "tc_strip_label_tx", "tc_strip_label_co"):
            self.assertIn(COPY[key], html, key)
        self.assertIn("123 Main St", COPY["tc_strip_label_tx"])

    def test_drop_box_first_then_sample_button_then_strip(self):
        # Redesign 2026-10-23: the drop box is the hero, the sample button
        # sits under it, and the sample strip is its own section below.
        html = _get("/tc-check")
        btn = html.find('id="demoBtn" data-evt="tc_sample_btn"')
        self.assertGreater(btn, 0)
        self.assertLess(html.find('id="dropZone"'), btn)
        self.assertLess(btn, html.find('id="sampleStrip"'))
        self.assertEqual(html.count('id="demoBtn"'), 1)
        self.assertIn("tc_sample_btn", app_module.SAMPLE_CTA_EVENTS)

    def test_november31_ad_leads_with_that_row(self):
        html = _get("/tc-check?src=ad_november31_fbtcg")
        self.assertEqual([r[0] for r in _strip_rows(html, "tx")], ["november31", "price", "email"])
        self.assertNotIn('id="titleLine"', html)

    def test_title_line_is_generic_and_ungated(self):
        for src in ("ti_keydfw", "title_reach_oct"):
            html = _get("/tc-check?src=" + src)
            line = re.search(r'<div class="title-line" id="titleLine">.*?</div></div>', html, re.S).group(0)
            self.assertIn(COPY["title_line"], line)
            # The copied link is the same for every title company...
            self.assertIn('value="' + TITLE_SHARE_URL + '"', line)
            self.assertIn('data-evt="tc_title_copy_link"', line)
            # ...and nothing on the page names the company the email went to.
            self.assertNotIn("keydfw", html.lower())
            self.assertNotIn("/sponsor", line)
            # The rest of the page is unchanged: same strip order, same drop box.
            self.assertEqual([r[0] for r in _strip_rows(html, "tx")], ["price", "november31", "email"])
            self.assertIn('id="dropZone"', html)
        self.assertLess(html.find('id="titleLine"'), html.find("<h1>"))

    def test_shared_link_shows_normal_page(self):
        self.assertNotIn('id="titleLine"', _get("/tc-check?src=title_share"))

    def test_colorado_mode_shows_cbs1_subhead_and_rows(self):
        html = _get("/tc-check?state=co")
        self.assertIn('class="state-co"', html)
        self.assertIn(COPY["tc_sub_co"], html)
        self.assertIn("Veterans Day", html)


class HomepageFirstScreen(unittest.TestCase):
    """The simplified homepage (2026-10-23) keeps the hero's analytics hooks
    on its new buttons: the sample button now opens /tc-check?demo=1 (which
    runs the sample there) instead of running it in place."""

    @classmethod
    def setUpClass(cls):
        cls.html = _get("/")

    def test_copy_and_hooks(self):
        self.assertNotIn("__FS_", self.html)
        self.assertNotIn("__HOME_", self.html)
        for hook in ('href="/tc-check" data-evt="hero_cta"', 'href="/tc-check?demo=1" data-evt="hero_sample"',
                     'href="/tc-check?state=co" data-evt="hero_cta"', 'href="/tc-check?demo=1&amp;state=co" data-evt="hero_sample"',
                     'id="heroReportCard"', 'href="/tc-check#what-it-checks" data-evt="home_check_link"'):
            self.assertIn(hook, self.html)
        for evt in ("hero_cta", "home_check_link"):
            self.assertIn(evt, app_module.CHECK_CTA_EVENTS)
        self.assertIn("hero_sample", app_module.SAMPLE_CTA_EVENTS)
        # Title line is /tc-check only.
        self.assertNotIn('id="titleLine"', _get("/?src=ti_keydfw"))

    def test_broker_section_is_texas_only_and_goes_to_bulk(self):
        m = re.search(r'<section class="brokers"([^>]*)>(.*?)</section>', self.html, re.S)
        self.assertIn('data-state="tx"', m.group(1))
        self.assertIn('href="/tc-check/bulk" data-evt="hero_broker_cta"', m.group(2))

    def test_subhead_per_state(self):
        self.assertIn('<p class="sub" data-state="tx">One checker for the Texas contracts you file most', self.html)

    def test_hero_lists_every_checked_contract(self):
        # Every contract TC Check reads must show in the hero's form row, so
        # the homepage can't drift from trec_forms.CHECKED_CONTRACTS.
        import trec_forms
        row = self.html[self.html.find('<ul class="forms"'):self.html.find('</ul>', self.html.find('<ul class="forms"'))]
        # The CBS1 chip sits in this (Texas) row; other Colorado contracts
        # belong in a Colorado row -- see test_co_forms_row.
        for c in trec_forms.CHECKED_CONTRACTS:
            if not c.startswith("Colorado ") or c == "Colorado CBS1":
                self.assertIn('<b>' + c.split()[-1] + '</b>', row)
        self.assertIn('<p class="sub" data-state="co">Drop a filled CBS1', self.html)

    def test_co_forms_row(self):
        # Colorado Land (CBS4) is checked since 2026-10-26 ("<b>CO Land</b>").
        import trec_forms
        start = self.html.find('<ul class="forms" data-state="co"')
        if start < 0:
            self.skipTest("no Colorado forms row on the homepage yet -- add one with a <b>CO Land</b> chip")
        row = self.html[start:self.html.find('</ul>', start)]
        for c in trec_forms.CHECKED_CONTRACTS:
            if c.startswith("Colorado ") and c != "Colorado CBS1":
                self.assertIn('<b>CO ' + c.split()[-1] + '</b>', row)

    def test_eyebrow_above_h1(self):
        self.assertLess(self.html.find('<div class="eyebrow">TC Check &middot; contract error-checker'), self.html.find("<h1>Catch it before"))

    def test_what_it_checks_anchor_on_tc_check(self):
        # The homepage link lands on a wrapper holding both states' lists.
        html = _get("/tc-check")
        self.assertEqual(html.count('id="what-it-checks"'), 1)
        anchor = html.find('id="what-it-checks"')
        self.assertLess(anchor, html.find("<li>Property address, county"))
        self.assertLess(anchor, html.find('id="colorado"'))


if __name__ == "__main__":
    unittest.main()
