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


class RecallControls(unittest.TestCase):
    """The half of the calibration that was missing.

    The gate was tuned only against questions that must return nothing, so it
    happily returned nothing for questions that must return something: ten
    live Meta events were trading while `search "Meta stock price"` reported
    no_live_market. Short entity names were the blind spot — every ticker and
    most proper nouns are under six letters.
    """

    ENTITIES = ["Meta stock price", "Tesla stock price", "Apple stock price",
                "Bitcoin price", "Nvidia stock price", "Trump approval",
                "Fed decision"]

    def test_common_entities_are_never_reported_as_having_no_market(self):
        for query in self.ENTITIES:
            with self.subTest(query):
                payload = pm_query.run_search(
                    [query], ["polymarket", "kalshi", "manifold"], limit=3)
                self.assertNotEqual(payload.get("verdict"), "no_live_market",
                                    f"{query!r} wrongly reported as having no market")
                self.assertTrue(payload["candidates"])

    def test_extra_phrasings_never_shrink_the_result(self):
        """SKILL.md asks for two or three keyword groups; following that
        advice used to make recall worse, not better."""
        one = pm_query.run_search(["Meta up or down"], ["polymarket"], limit=3)
        three = pm_query.run_search(
            ["Meta up or down", "Meta stock price", "Meta share decline"],
            ["polymarket"], limit=3)
        self.assertGreaterEqual(len(three["candidates"]), len(one["candidates"]))

    def test_price_ladders_come_back_whole(self):
        """A 14-rung ladder is the shape that answers "how far", and per-venue
        trimming used to slice it to four rows with no indication."""
        payload = pm_query.run_search(["Meta Platforms"], ["polymarket"], limit=3)
        biggest = max((e["outcomes_returned"] for e in payload.get("events", [])), default=0)
        self.assertGreater(biggest, 5, "ladders should not be trimmed to a handful of rungs")


class OutcomeLabelControls(unittest.TestCase):
    def test_every_candidate_says_which_outcome_its_price_belongs_to(self):
        payload = pm_query.run_search(
            ["Meta up or down", "US recession 2026"],
            ["polymarket", "kalshi", "manifold"], limit=3)
        self.assertTrue(payload["candidates"])
        for m in payload["candidates"]:
            self.assertIsNotNone(m["probability_of"], f"unlabelled price: {m['title']}")
            self.assertTrue(m["outcome_prices"])


class SpotControls(unittest.TestCase):
    def test_spot_anchors_a_price_ladder(self):
        quote = pm_query.fetch_spot("META")
        self.assertGreater(quote["price"], 0)
        self.assertEqual(quote["symbol"], "META")
        self.assertIn("fetched_at", quote)


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
        self.assertIn("fetched_at", market)

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


class KalshiTrendControls(unittest.TestCase):
    """`detail` promised a 30-day trend and delivered it only for Polymarket,
    leaving the answer format's trend line unfillable for the venue that
    often has the deeper book."""

    def test_kalshi_detail_now_carries_history(self):
        payload = pm_query.run_search(["Fed decision"], ["kalshi"], limit=2)
        self.assertTrue(payload["candidates"])
        market = pm_query.detail_kalshi(payload["candidates"][0]["id"])
        self.assertGreater(len(market.get("history") or []), 5,
                           f"no Kalshi history: {market.get('history_error')}")
        self.assertIsNotNone(market["prob_24h_change"])


class CompareControls(unittest.TestCase):
    def test_compare_computes_the_spread_it_is_asked_for(self):
        poly = pm_query.run_search(["US recession 2026"], ["polymarket"], limit=1)
        kalshi = pm_query.run_search(["recession this year"], ["kalshi"], limit=1)
        self.assertTrue(poly["candidates"] and kalshi["candidates"])
        payload = pm_query.run_compare([
            f"polymarket:{poly['candidates'][0]['id']}",
            f"kalshi:{kalshi['candidates'][0]['id']}"])
        self.assertIn("spread_pp", payload)
        self.assertIsNotNone(payload["spread_pp"])
        self.assertIn("agree", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
