"""Parser: financing words and county names (trec-qa, 2026-10-24).

Bugs, live on SMS:
- "USDA", "Texas Veterans", "reverse mortgage" stayed in the address:
  "725k 3% 21day USDA 123 Main St, Austin, TX" -> "Usda 123 Main St" (rejected),
  "... 123 Main St, Austin, TX USDA" -> "123 Main St, , Usda" (and drafted).
- County false positives: "123 Denton Dr, Austin" -> County of = Denton;
  "123 Main St, Dallas, TX 75201" -> County of = Dallas (the city).

    python3 -m unittest tests.test_parser_financing_county
"""
import io
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "test.db")
os.environ["OFFER_OUTPUT_DIR"] = os.path.join(_TMP, "offers")
os.makedirs(os.environ["OFFER_OUTPUT_DIR"], exist_ok=True)
for _k in ("APIFY_API_TOKEN", "TWILIO_AUTH_TOKEN", "RESEND_API_KEY", "SENDGRID_API_KEY"):
    os.environ[_k] = ""

import app as app_module  # noqa: E402
import financing_addendum  # noqa: E402
from parser import FINANCING_RE, parse_offer_sms  # noqa: E402
from pypdf import PdfReader  # noqa: E402

# Every spelling FINANCING_RE takes for the three types that broke, plus the
# ones that already worked, so a new keyword can't skip the address strip.
FINANCING_WORDS = [
    ("USDA", "usda"), ("usda loan", "usda"),
    ("Texas Veterans", "texas_veterans"), ("texas vet", "texas_veterans"),
    ("Texas Veteran loan", "texas_veterans"),
    ("reverse mortgage", "reverse_mortgage"), ("Reverse Mortgage", "reverse_mortgage"),
    ("FHA", "fha"), ("FHA loan", "fha"), ("VA", "va"), ("conventional", "conventional"),
]


class FinancingWordsLeaveTheAddress(unittest.TestCase):
    def test_before_and_after_address(self):
        for word, ftype in FINANCING_WORDS:
            for sms in (f"725k 3% 21day {word} 123 Main St, Austin, TX",
                        f"725k 3% 21day 123 Main St, Austin, TX {word}"):
                with self.subTest(sms=sms):
                    r = parse_offer_sms(sms)
                    self.assertNotIn("error", r)
                    self.assertEqual(r["address"], "123 Main St")
                    self.assertEqual(r.get("city"), "Austin")
                    self.assertEqual(r.get("financing_type"), ftype)

    def test_every_financing_regex_type_is_covered(self):
        # Derive the types from FINANCING_RE itself: if someone adds one,
        # this fails until it's in FINANCING_WORDS above.
        alts = FINANCING_RE.pattern.split("(", 1)[1].rsplit(")", 1)[0]
        n_types = len([a for a in alts.split("|") if a and not a.startswith("?")])
        covered = {t for _, t in FINANCING_WORDS} | {"cash"}
        self.assertEqual(len(covered), n_types)

    def test_end_to_end_draft_and_40_11(self):
        # SMS -> draft -> confirmation label -> 40-11 box and amount, for the
        # three types the page now lists. Box names are the rect-verified
        # financing_addendum.FIELD_MAP entries (2026-09-09), not the /T text.
        for word, ftype, label in [("USDA", "usda", "USDA"),
                                   ("Texas Veterans", "texas_veterans", "Texas Veterans"),
                                   ("reverse mortgage", "reverse_mortgage", "Reverse Mortgage")]:
            for sms in (f"725k 3% 21day {word} 123 Main St, Austin, TX",
                        f"725k 3% 21day 123 Main St, Austin, TX {word}"):
                with self.subTest(sms=sms):
                    p, err, _ = app_module.build_offer_draft(sms, "+15125550177")
                    self.assertIsNone(err)
                    self.assertEqual((p["address"], p["city"], p["financing_type"]), ("123 Main St", "Austin", ftype))
                    conf = app_module.format_offer_confirmation(p)
                    self.assertIn(f"Financing: {label}\n", conf)
                    self.assertIn("123 Main St, Austin, TX\n", conf)

                    fields = PdfReader(io.BytesIO(financing_addendum.fill_financing_addendum(p))).get_fields()
                    # fill_financing_addendum namespaces every field "FA_" before the merge.
                    fm = {k: "FA_" + v for k, v in financing_addendum.FIELD_MAP.items()}
                    self.assertNotIn(str(fields[fm[ftype]].get("/V")), ("", "None", "/Off"))
                    self.assertEqual(fields[fm[ftype + "_amount"]].get("/V"), "703,250")
                    self.assertEqual(fields[fm["address_p1"]].get("/V"), "123 Main St, Austin, TX")
                    for other in ("conventional", "fha", "va", "usda", "texas_veterans", "reverse_mortgage"):
                        if other != ftype:
                            self.assertIn(str(fields[fm[other]].get("/V")), ("", "None", "/Off"), other)


class CountyOnlyWhenItIsTheCounty(unittest.TestCase):
    CASES = [
        # (sms, expected county)
        ("725k 3% 21day 123 Denton Dr, Austin, TX", None),           # street name
        ("1.2m 20% FHA 45 days 10-day option HOA 123 Main St, Dallas, TX 75201", None),  # the city
        ("725k 3% 21day 123 Main St, Denton, TX", None),             # city slot
        ("725k 3% 21day 123 Dallas Pkwy, Plano, TX", None),
        ("725k 3% 21day 123 Main St, El Paso, TX 79901", None),
        ("Travis County 123 Main St, Austin, TX 725k 3% 21day", "Travis"),
        ("725k 3% 21day Travis County 123 Main St, Austin, TX", "Travis"),
        ("725k 3% 21day 123 Main St, Fort Worth, TX Tarrant County", "Tarrant"),
        ("725k 3% 21day 123 Main St, Austin, Travis co.", "Travis"),
        ("725k 3% 21day Travis 123 Main St, Austin, TX", "Travis"),  # standalone, before the street
        ("725k 3% 21day 123 Main St, Austin, TX Travis", "Travis"),  # standalone, after the state
        ("725k 3% 21day Dallas 123 Main St, Dallas, TX", "Dallas"),  # the first Dallas is the county
        ("725k 3% 21day Dallas County 123 Main St, Dallas, TX", "Dallas"),
    ]

    def test_cases(self):
        for sms, county in self.CASES:
            with self.subTest(sms=sms):
                r = parse_offer_sms(sms)
                self.assertNotIn("error", r)
                self.assertEqual(r.get("county"), county)
                self.assertTrue(r["address"] in ("123 Main St", "123 Denton Dr", "123 Dallas Pkwy"), r["address"])

    def test_summary_shows_county_only_when_set(self):
        p, err, _ = app_module.build_offer_draft("Travis County 123 Main St, Austin, TX 725k 3% 21day", "+15125550178")
        self.assertIsNone(err)
        self.assertIn("123 Main St, Austin, TX\nCounty: Travis\nPrice $725,000", app_module.format_offer_confirmation(p))
        p, err, _ = app_module.build_offer_draft("725k 3% 21day 123 Denton Dr, Austin, TX", "+15125550178")
        self.assertIsNone(err)
        self.assertNotIn("County", app_module.format_offer_confirmation(p))


if __name__ == "__main__":
    unittest.main()
