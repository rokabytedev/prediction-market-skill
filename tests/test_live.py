#!/usr/bin/env python3
"""End-to-end checks against the live venue APIs.

Kept out of the default `make test` run because it needs network and the
upstream data moves. Run with `make test-live`.

The negative controls are the point of this file. A skill that answers
"how likely is X?" is only trustworthy if it reliably declines to answer when
no market exists — and the first implementation failed exactly that check,
returning the Los Angeles mayoral race for "will I get promoted next year".
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "prediction-market", "scripts"))

import pm_query  # noqa: E402


class NegativeControls(unittest.TestCase):
    """Questions no venue lists. Every one must come back empty."""

    CASES = [
        ("personal promotion", ["will I get promoted next year at my job",
                      "my personal promotion 2027"]),
        ("pet talent", ["will my cat learn to play piano by 2027"]),
        ("chinese property", ["Hangzhou house price drop", "china property price fall"]),
    ]

    def test_no_market_questions_return_no_market(self):
        for label, keywords in self.CASES:
            with self.subTest(label):
                payload = pm_query.run_search(
                    keywords, ["polymarket", "kalshi", "manifold"], limit=4)
                self.assertEqual(payload.get("verdict"), "no_live_market",
                                 f"{label}: {payload.get('candidates')}")
                self.assertEqual(payload["candidates"], [])

    def test_the_fuzzy_matches_really_were_offered(self):
        """Guard against a vacuous pass: if the venues stopped returning
        anything at all, the controls above would pass without proving that
        the relevance gate does any work."""
        payload = pm_query.run_search(
            ["will my cat learn to play piano by 2027"],
            ["polymarket", "kalshi", "manifold"], limit=4)
        self.assertGreater(payload.get("dropped_as_irrelevant", 0), 10)


class PositiveControls(unittest.TestCase):
    def search(self, *keywords):
        return pm_query.run_search(list(keywords),
                                   ["polymarket", "kalshi", "manifold"], limit=4)

    def test_finds_a_live_fed_market(self):
        payload = self.search("Fed rate cut September", "FOMC September decision")
        self.assertTrue(payload["candidates"])
        for m in payload["candidates"]:
            self.assertIsNotNone(m["probability"])
            self.assertTrue(0.0 <= m["probability"] <= 1.0)

    def test_reaches_both_real_money_venues_on_a_shared_question(self):
        payload = self.search("US recession 2026", "recession this year")
        venues = {m["source"] for m in payload["candidates"]}
        self.assertIn("polymarket", venues)
        self.assertIn("kalshi", venues)

    def test_word_form_differences_still_match(self):
        """Kalshi lists "Will Trump be impeached?" for "Trump impeachment"."""
        payload = self.search("Trump impeachment")
        self.assertTrue(payload["candidates"])

    def test_settled_markets_never_surface(self):
        payload = self.search("Fed rate cut September", "US recession 2026")
        for m in payload["candidates"]:
            self.assertNotIn(m["probability"], (0.0, 1.0),
                             f"settled market leaked: {m['title']} / {m['outcome']}")


class DetailControls(unittest.TestCase):
    def test_polymarket_detail_carries_trend_depth_and_rules(self):
        payload = pm_query.run_search(["US recession 2026"], ["polymarket"], limit=1)
        self.assertTrue(payload["candidates"], "no live Polymarket recession market")
        market = pm_query.detail_polymarket(payload["candidates"][0]["id"])
        self.assertIsNotNone(market["probability"])
        self.assertTrue(market["rules"])
        self.assertGreater(len(market.get("history") or []), 5)
        self.assertIsNotNone(market["participants"])
        self.assertEqual(market["participants_label"], "holders")

    def test_gamma_identity_is_verified(self):
        """A mistyped filter returns someone else's market with a 200."""
        with self.assertRaises(LookupError):
            pm_query.detail_polymarket("0x" + "0" * 64)

    def test_deep_kalshi_market_is_not_flagged_thin(self):
        payload = pm_query.run_search(["Fed decision September"], ["kalshi"], limit=1)
        self.assertTrue(payload["candidates"])
        market = pm_query.detail_kalshi(payload["candidates"][0]["id"])
        self.assertFalse([f for f in market["flags"] if "Shallow book" in f],
                         f"bogus depth warning: {market['flags']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
