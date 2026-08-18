"""Unit tests for pm_query normalization.

Every fixture is a real API response captured 2026-08-18, not hand-written,
so these tests fail if a provider changes its schema out from under us.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "prediction-market", "scripts"))

import pm_query  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


class PolymarketEventTest(unittest.TestCase):
    """Event 106884 holds 5 resolved sub-markets and 3 live ones.

    The live ones all carry a stale endDate in the past, so date alone
    cannot decide liveness.
    """

    def setUp(self):
        self.event = load("poly_event_mixed_live_and_resolved.json")
        self.markets = pm_query.parse_polymarket_event(self.event)

    def test_drops_resolved_submarkets(self):
        titles = [m["outcome"] for m in self.markets]
        for dead in ("June Meeting", "January Meeting", "April Meeting",
                     "March Meeting", "July Meeting"):
            self.assertNotIn(dead, titles)

    def test_keeps_live_submarkets_despite_stale_end_date(self):
        titles = [m["outcome"] for m in self.markets]
        for live in ("September Meeting", "October Meeting", "December Meeting"):
            self.assertIn(live, titles)

    def test_probability_is_yes_price(self):
        dec = next(m for m in self.markets if m["outcome"] == "December Meeting")
        self.assertAlmostEqual(dec["probability"], 0.135, places=4)

    def test_volume_is_per_submarket_not_event_total(self):
        """Event-level volume (~3.1M) sums in the dead sub-markets.

        Using it would wildly overstate how credible a live sub-market is.
        """
        event_volume = self.event["volume"]
        for m in self.markets:
            self.assertIsNotNone(m["volume_usd"])
            self.assertLess(m["volume_usd"], event_volume)

    def test_stale_end_date_is_flagged_not_filtered(self):
        dec = next(m for m in self.markets if m["outcome"] == "December Meeting")
        self.assertTrue(dec["end_date_passed"])

    def test_exact_zero_one_price_counts_as_resolved(self):
        self.assertTrue(pm_query.is_polymarket_resolved(
            {"closed": True, "outcomePrices": '["0", "1"]'}))
        self.assertFalse(pm_query.is_polymarket_resolved(
            {"closed": False, "outcomePrices": '["0.135", "0.865"]'}))

    def test_low_price_live_market_is_not_treated_as_resolved(self):
        """0.95% is a real market view, not a settled market."""
        self.assertFalse(pm_query.is_polymarket_resolved(
            {"closed": False, "outcomePrices": '["0.0095", "0.9905"]'}))


class PolymarketSearchTest(unittest.TestCase):
    def setUp(self):
        self.raw = load("poly_search_polluted.json")

    def test_closed_events_are_removed(self):
        events = pm_query.filter_polymarket_search(self.raw)
        self.assertTrue(events, "fixture should still yield live events")
        for e in events:
            self.assertFalse(e["closed"])

    def test_fixture_actually_contains_pollution(self):
        """Guard: if the fixture stops containing closed events the test above
        would pass vacuously."""
        self.assertTrue(any(e.get("closed") for e in self.raw["events"]))


class KalshiTest(unittest.TestCase):
    def test_v2_uses_new_dollar_fields(self):
        raw = load("kalshi_markets_new_fields.json")
        markets = pm_query.parse_kalshi_v2_markets(raw)
        self.assertTrue(markets)
        priced = [m for m in markets if m["probability"] is not None]
        self.assertTrue(priced, "should read last_price_dollars")
        for m in priced:
            self.assertGreaterEqual(m["probability"], 0.0)
            self.assertLessEqual(m["probability"], 1.0)

    def test_legacy_fields_are_absent_from_response(self):
        """Documents why the parser must not read yes_bid/last_price/volume."""
        raw = load("kalshi_markets_new_fields.json")
        first = raw["markets"][0]
        for legacy in ("yes_bid", "yes_ask", "last_price", "volume", "open_interest"):
            self.assertNotIn(legacy, first)

    def test_v2_reads_volume_and_open_interest(self):
        raw = load("kalshi_markets_new_fields.json")
        markets = pm_query.parse_kalshi_v2_markets(raw)
        self.assertTrue(any(m["volume_usd"] for m in markets))
        self.assertTrue(any(m["open_interest_usd"] for m in markets))

    def test_search_parses_nested_prices(self):
        raw = load("kalshi_search.json")
        markets = pm_query.parse_kalshi_search(raw)
        self.assertTrue(markets)
        ratecut = next(m for m in markets if m["id"] == "KXRATECUT-26DEC31")
        self.assertAlmostEqual(ratecut["probability"], 0.159, places=3)
        self.assertEqual(ratecut["source"], "kalshi")
        self.assertIn("kalshi.com", ratecut["url"])


class ManifoldTest(unittest.TestCase):
    def setUp(self):
        self.markets = pm_query.parse_manifold_search(load("manifold_search.json"))

    def test_reads_probability_and_bettors(self):
        m = self.markets[0]
        self.assertIsInstance(m["probability"], float)
        self.assertIsInstance(m["participants"], int)
        self.assertEqual(m["participants_label"], "bettors")

    def test_always_flagged_as_play_money(self):
        for m in self.markets:
            self.assertTrue(any("Play-money" in f for f in m["flags"]))


class CredibilityTest(unittest.TestCase):
    def base(self, **kw):
        m = {
            "source": "polymarket", "probability": 0.5,
            "volume_usd": 1_000_000.0, "volume_24h_usd": 5_000.0,
            "liquidity_usd": 50_000.0, "flags": [],
        }
        m.update(kw)
        return m

    def test_thin_volume_flagged(self):
        flags = pm_query.credibility_flags(self.base(volume_usd=1_000.0))
        self.assertTrue(any("Thin market" in f for f in flags))

    def test_thin_liquidity_flagged(self):
        flags = pm_query.credibility_flags(self.base(liquidity_usd=100.0))
        self.assertTrue(any("Shallow book" in f for f in flags))

    def test_no_recent_trading_flagged(self):
        flags = pm_query.credibility_flags(self.base(volume_24h_usd=0.0))
        self.assertTrue(any("stale" in f for f in flags))

    def test_healthy_market_has_no_warnings(self):
        self.assertEqual(pm_query.credibility_flags(self.base()), [])

    def test_near_certain_price_noted(self):
        flags = pm_query.credibility_flags(self.base(probability=0.99))
        self.assertTrue(any("settled" in f for f in flags))


class HoldersTest(unittest.TestCase):
    def test_dedupes_across_tokens(self):
        raw = [
            {"token": "a", "holders": [{"proxyWallet": "0x1"}, {"proxyWallet": "0x2"}]},
            {"token": "b", "holders": [{"proxyWallet": "0x2"}, {"proxyWallet": "0x3"}]},
        ]
        count, truncated = pm_query.count_holders(raw, limit=500)
        self.assertEqual(count, 3)
        self.assertFalse(truncated)

    def test_reports_truncation_when_page_is_full(self):
        raw = [{"token": "a", "holders": [{"proxyWallet": f"0x{i}"} for i in range(500)]}]
        count, truncated = pm_query.count_holders(raw, limit=500)
        self.assertTrue(truncated)


class DivergenceTest(unittest.TestCase):
    def test_flags_cross_platform_gap_between_real_money_markets(self):
        a = {"source": "polymarket", "probability": 0.73}
        b = {"source": "kalshi", "probability": 0.62}
        self.assertTrue(pm_query.divergence_note([a, b]))

    def test_ignores_play_money_when_comparing(self):
        a = {"source": "polymarket", "probability": 0.73}
        b = {"source": "manifold", "probability": 0.20}
        self.assertIsNone(pm_query.divergence_note([a, b]))

    def test_silent_when_markets_agree(self):
        a = {"source": "polymarket", "probability": 0.73}
        b = {"source": "kalshi", "probability": 0.71}
        self.assertIsNone(pm_query.divergence_note([a, b]))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CandidateSelectionTest(unittest.TestCase):
    def test_drops_candidates_without_a_probability(self):
        """A candidate with no number cannot answer the question."""
        markets = [
            {"source": "manifold", "id": "a", "probability": None},
            {"source": "manifold", "id": "b", "probability": 0.4},
        ]
        kept = pm_query.drop_unpriced(markets)
        self.assertEqual([m["id"] for m in kept], ["b"])


class KalshiVolumeAttributionTest(unittest.TestCase):
    def test_event_volume_is_not_reported_as_submarket_volume(self):
        """Same mistake as Polymarket's event-level total: the venue's recent
        volume covers every sibling market, so it must not be presented as
        this market's own 24h figure."""
        raw = load("kalshi_search.json")
        markets = pm_query.parse_kalshi_search(raw)
        by_event = {}
        for m in markets:
            by_event.setdefault(m["title"], []).append(m)
        siblings = max(by_event.values(), key=len)
        self.assertGreater(len(siblings), 1, "need an event with several markets")
        for m in siblings:
            self.assertIsNone(m["volume_24h_usd"])
        self.assertTrue(all(m.get("event_volume_24h_usd") for m in siblings))

    def test_active_event_does_not_trigger_stale_warning(self):
        market = {"source": "kalshi", "probability": 0.5, "volume_usd": 1e6,
                  "volume_24h_usd": None, "event_volume_24h_usd": 500_000.0,
                  "liquidity_usd": None}
        self.assertFalse(any("stale" in f for f in pm_query.credibility_flags(market)))


class DivergenceScopeTest(unittest.TestCase):
    def test_search_does_not_claim_divergence_across_unmatched_outcomes(self):
        """Comparing 'Cut 25bps' on one venue with 'No change' on another
        yields a meaningless 70-point 'divergence'."""
        payload = pm_query.build_search_payload(
            keywords=["x"], sources=["polymarket", "kalshi"], errors={},
            markets=[
                {"source": "kalshi", "id": "a", "outcome": "Cut 25bps", "probability": 0.01},
                {"source": "polymarket", "id": "b", "outcome": "No change", "probability": 0.71},
            ])
        self.assertNotIn("divergence", payload)

    def test_compare_reports_gap_for_markets_the_caller_matched(self):
        note = pm_query.divergence_note([
            {"source": "polymarket", "probability": 0.71},
            {"source": "kalshi", "probability": 0.62},
        ])
        self.assertIsNotNone(note)


class RoundingTest(unittest.TestCase):
    def test_probability_deltas_are_not_float_noise(self):
        raw = load("kalshi_search.json")
        for m in pm_query.parse_kalshi_search(raw):
            delta = m["prob_24h_change"]
            if delta is not None:
                self.assertEqual(delta, round(delta, 4))


class RelevanceGateTest(unittest.TestCase):
    """The venues' search endpoints are fuzzy and always return something.

    Live examples that motivated this gate: "will I get promoted next year"
    returned the Los Angeles mayoral race, and "will my cat learn to play
    piano" returned Super Bowl halftime performers. Handing those to the
    model as candidates is how a query-only skill starts inventing answers.
    """

    def market(self, title, outcome="Yes"):
        return {"source": "kalshi", "id": title, "title": title,
                "outcome": outcome, "probability": 0.5}

    def test_rejects_unrelated_market(self):
        kept = pm_query.filter_relevant(
            [self.market("Los Angeles Mayor winner?", "Nithya Raman")],
            ["will I get promoted next year at my job"])
        self.assertEqual(kept, [])

    def test_rejects_halftime_show_for_cat_piano_question(self):
        kept = pm_query.filter_relevant(
            [self.market("Super Bowl halftime performer?", "Drake")],
            ["will my cat learn to play piano by 2027"])
        self.assertEqual(kept, [])

    def test_keeps_genuine_match(self):
        kept = pm_query.filter_relevant(
            [self.market("Fed decision in September?", "Fed maintains rate")],
            ["FOMC September decision"])
        self.assertEqual(len(kept), 1)

    def test_keeps_short_but_meaningful_token(self):
        kept = pm_query.filter_relevant(
            [self.market("AI bubble burst by...?", "2026")], ["AI bubble"])
        self.assertEqual(len(kept), 1)

    def test_year_alone_is_not_relevance(self):
        """Otherwise every market closing in 2027 matches any question that
        happens to mention 2027."""
        kept = pm_query.filter_relevant(
            [self.market("Fed rate cut before 2027?", "Cuts")],
            ["my personal promotion 2027"])
        self.assertEqual(kept, [])

    def test_fails_open_when_query_is_all_stopwords(self):
        markets = [self.market("Anything at all?")]
        self.assertEqual(pm_query.filter_relevant(markets, ["will it be"]), markets)

    def test_records_which_tokens_matched(self):
        kept = pm_query.filter_relevant(
            [self.market("Fed decision in September?")], ["FOMC September decision"])
        self.assertIn("september", kept[0]["matched"])


class RelevanceStrengthTest(unittest.TestCase):
    """One shared generic word is not a match.

    Live leaks this closes: "will I get promoted next year" matched "Blue wave
    this year?" on the single word `year`; "Hangzhou house price drop" matched
    "Which party will win the U.S. House?" on `house`; "US recession 2026"
    matched "2Y US Treasury yield today?" on `us`.
    """

    def market(self, title, outcome="Yes"):
        return {"source": "kalshi", "id": title, "title": title,
                "outcome": outcome, "probability": 0.5}

    def test_single_generic_token_is_not_enough(self):
        kept = pm_query.filter_relevant(
            [self.market("Which party will win the U.S. House?", "Republican Party")],
            ["Hangzhou house price drop 2027"])
        self.assertEqual(kept, [])

    def test_treasury_yield_is_not_a_recession_market(self):
        kept = pm_query.filter_relevant(
            [self.market("2Y US Treasury yield today?", "4.25% or above")],
            ["US recession 2026"])
        self.assertEqual(kept, [])

    def test_real_recession_market_survives(self):
        kept = pm_query.filter_relevant(
            [self.market("US recession by end of 2026?", "Yes")], ["US recession 2026"])
        self.assertEqual(len(kept), 1)

    def test_ai_company_ranking_is_not_an_ai_bubble_market(self):
        kept = pm_query.filter_relevant(
            [self.market("Which AI company will have the best model?", "Anthropic")],
            ["AI bubble burst"])
        self.assertEqual(kept, [])

    def test_real_ai_bubble_market_survives(self):
        kept = pm_query.filter_relevant(
            [self.market("AI bubble burst by...?", "2026")], ["AI bubble burst"])
        self.assertEqual(len(kept), 1)

    def test_one_word_query_still_matches_on_one_word(self):
        """Requiring two matches would make a one-word question unanswerable."""
        kept = pm_query.filter_relevant(
            [self.market("US recession by end of 2026?")], ["recession"])
        self.assertEqual(len(kept), 1)

    def test_bare_temporal_words_carry_no_signal(self):
        self.assertNotIn("year", pm_query.content_tokens("next year"))
        self.assertNotIn("today", pm_query.content_tokens("today"))


class HistoryDerivedChangeTest(unittest.TestCase):
    """Gamma's per-market detail response carries only oneMonthPriceChange.

    The 1-day and 1-week deltas exist on the event-nested shape but vanish on
    detail, so derive them from the daily price history we already fetch
    rather than reporting a blank trend on the most informative call.
    """

    def history(self, prices):
        return [{"date": f"2026-08-{i + 1:02d}", "p": p} for i, p in enumerate(prices)]

    def test_derives_one_day_and_one_week_change(self):
        hist = self.history([0.20, 0.19, 0.18, 0.17, 0.16, 0.15, 0.14, 0.12])
        day, week = pm_query.changes_from_history(hist)
        self.assertAlmostEqual(day, -0.02, places=4)
        # 08-08 back to 08-01 is a full week: 0.12 - 0.20
        self.assertAlmostEqual(week, -0.08, places=4)

    def test_short_history_yields_none_rather_than_a_wrong_number(self):
        day, week = pm_query.changes_from_history(self.history([0.2, 0.18]))
        self.assertAlmostEqual(day, -0.02, places=4)
        self.assertIsNone(week)

    def test_empty_history_is_safe(self):
        self.assertEqual(pm_query.changes_from_history([]), (None, None))


class StemmingTest(unittest.TestCase):
    """Exact token matching loses real markets to ordinary word forms:
    Kalshi lists "Will Trump be impeached?" for a question phrased as
    "Trump impeachment"."""

    def test_word_forms_collapse_to_one_stem(self):
        for a, b in [("impeachment", "impeached"), ("recession", "recessions"),
                     ("cuts", "cut"), ("rates", "rate"), ("bubbles", "bubble")]:
            self.assertEqual(pm_query.stem(a), pm_query.stem(b), f"{a} vs {b}")

    def test_stemming_does_not_collapse_unrelated_words(self):
        self.assertNotEqual(pm_query.stem("recession"), pm_query.stem("recess"))

    def test_impeachment_query_finds_impeached_market(self):
        kept = pm_query.filter_relevant(
            [{"source": "kalshi", "id": "x", "title": "Will Trump be impeached?",
              "outcome": "Yes", "probability": 0.1}],
            ["Trump impeachment"])
        self.assertEqual(len(kept), 1)


class DistinctiveTokenTest(unittest.TestCase):
    """Kalshi titles its recession market "Recession this year?" — the only
    shared word with "US recession 2026" is the one that matters. Requiring
    two matches drops it, so a single distinctive word has to be enough."""

    def market(self, title, outcome="Yes"):
        return {"source": "kalshi", "id": title, "title": title,
                "outcome": outcome, "probability": 0.5}

    def test_single_distinctive_word_is_enough(self):
        kept = pm_query.filter_relevant(
            [self.market("Recession this year?")], ["US recession 2026"])
        self.assertEqual(len(kept), 1)

    def test_single_generic_word_is_still_not_enough(self):
        for title, query in [
            ("Which party will win the U.S. House?", "Hangzhou house price drop"),
            ("2Y US Treasury yield today?", "US recession 2026"),
            ("Which AI company will have the best model?", "AI bubble burst"),
        ]:
            self.assertEqual(pm_query.filter_relevant([self.market(title)], [query]), [],
                             f"{query!r} should not match {title!r}")


class KalshiDepthTest(unittest.TestCase):
    """Kalshi reports liquidity_dollars as 0.0000 on every market, including
    one with $4.3M traded and $3.1M open interest. Treating that as real
    depth stamps a bogus 'thin liquidity' warning on the deepest markets."""

    def test_zero_liquidity_is_treated_as_unavailable(self):
        raw = load("kalshi_markets_new_fields.json")
        for m in pm_query.parse_kalshi_v2_markets(raw):
            self.assertIsNone(m["liquidity_usd"])

    def test_deep_kalshi_market_gets_no_thin_warning(self):
        market = {"source": "kalshi", "probability": 0.71, "volume_usd": 4_284_463.0,
                  "liquidity_usd": None, "open_interest_usd": 3_133_705.0,
                  "event_volume_24h_usd": 100.0}
        self.assertEqual(pm_query.credibility_flags(market), [])

    def test_shallow_kalshi_market_still_warns(self):
        market = {"source": "kalshi", "probability": 0.5, "volume_usd": 1_000_000.0,
                  "liquidity_usd": None, "open_interest_usd": 900.0,
                  "event_volume_24h_usd": 100.0}
        self.assertTrue(any("open interest" in f for f in pm_query.credibility_flags(market)))
