"""Regression: closing-timeframe phrases must not eat the street number.

Bug (2026-10-24): "725k 3% close in 30 days 123 Main St, Houston, TX" parsed
the address as "Main St" -- the bare "30 days" strip ran first, then the
"close in <n>" strip matched "close in 123".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import _parse_address, _parse_days, parse_offer_sms  # noqa: E402


class ClosePhraseKeepsStreetNumber(unittest.TestCase):
    CASES = [
        # (sms, expected address, expected close_days)
        ("725k 3% close in 30 days 123 Main St, Houston, TX", "123 Main St", 30),
        ("725k 3% close in 30 123 Main St, Houston, TX", "123 Main St", 30),
        ("725k 3% closing in 30 days 123 Main St", "123 Main St", 30),
        ("725k 3% closing in 30 123 Main St", "123 Main St", 30),
        ("725k 3% 30 day close 123 Main St", "123 Main St", 30),
        ("21 day close 123 Main St", "123 Main St", 21),
        ("725k 3% 21-day closing 123 Main St", "123 Main St", 21),
        ("21-day close 123 Main St", "123 Main St", 21),
        ("725k 3% 21-day 123 Main St", "123 Main St", 21),
        ("725k 3% 21day 123 Main St", "123 Main St", 21),
        ("725k 3% 21 days 123 Main St", "123 Main St", 21),
        ("123 Main St 725k 3% close in 30 days", "123 Main St", 30),
        ("500k 123 Main St 30 days", "123 Main St", 30),
        ("725k 3% 21day 10-day inspection 123 Main St", "123 Main St", 21),
    ]

    def test_address_and_days(self):
        for sms, addr, days in self.CASES:
            with self.subTest(sms=sms):
                self.assertEqual(_parse_address(sms), addr)
                self.assertEqual(_parse_days(sms), days)

    def test_full_parse(self):
        r = parse_offer_sms("725k 3% close in 30 days 123 Main St, Houston, TX")
        self.assertNotIn("error", r)
        self.assertEqual(r["close_days"], 30)
        self.assertTrue(r["address"].startswith("123 Main St"), r["address"])


class NoRegressionOnTrickyAddresses(unittest.TestCase):
    def test_place_name_street(self):
        self.assertEqual(_parse_address("123 Dallas Pkwy 500k 3% 21day"), "123 Dallas Pkwy")
        self.assertEqual(_parse_address("500k 3% close in 30 days 123 Dallas Pkwy"), "123 Dallas Pkwy")

    def test_million_street(self):
        self.assertEqual(_parse_address("100 Million Dr 1.2m 5% close in 30"), "100 Million Dr")
        self.assertEqual(_parse_address("1.2m 5% 30 day close 100 Million Dr"), "100 Million Dr")

    def test_street_number_after_price(self):
        self.assertEqual(_parse_address("725k 123 Main St 3% 21day"), "123 Main St")
        self.assertEqual(_parse_address("725,000 123 Main St 3% close in 21"), "123 Main St")


if __name__ == "__main__":
    unittest.main()
