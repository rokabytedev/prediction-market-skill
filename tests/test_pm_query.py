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
        self.assertTrue(any("near certain" in f for f in flags))


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


# ==========================================================================
# Regression suite for the defects found by testing the skill on
# "Will Meta fall further, and how far?" (2026-08-18).
# ==========================================================================


class OutcomeLabelTest(unittest.TestCase):
    """A price with no label attached can be read backwards.

    "Meta (META) Up or Down on August 19?" carries outcomes ["Up","Down"] and
    prices ["0.555","0.445"]. Reporting a bare 0.555 on a question phrased
    "will it fall" invites the exact opposite of the market's view.
    """

    def test_non_yes_no_binary_says_which_outcome_the_price_is(self):
        event = load("poly_event_up_down_binary.json")
        m = pm_query.parse_polymarket_event(event)[0]
        self.assertEqual(m["probability_of"], "Up")
        self.assertEqual([o["label"] for o in m["outcome_prices"]], ["Up", "Down"])
        self.assertAlmostEqual(
            dict((o["label"], o["p"]) for o in m["outcome_prices"])["Down"],
            1 - m["probability"], places=3)

    def test_yes_no_market_is_labelled_yes(self):
        event = load("poly_event_mixed_live_and_resolved.json")
        for m in pm_query.parse_polymarket_event(event):
            self.assertEqual(m["probability_of"], "Yes")

    def test_kalshi_and_manifold_are_labelled_too(self):
        k = pm_query.parse_kalshi_v2_markets(load("kalshi_markets_new_fields.json"))
        self.assertTrue(all(m["probability_of"] == "Yes" for m in k))
        mf = pm_query.parse_manifold_search(load("manifold_search.json"))
        self.assertTrue(all(m["probability_of"] == "Yes" for m in mf))


class RelevanceRecallTest(unittest.TestCase):
    """The gate returned no_live_market for "Meta stock price" while ten live
    Meta events were trading. Short entity names were the blind spot: `meta`
    is four letters, so it never counted as distinctive, and generic finance
    words were doing the matching instead."""

    def market(self, title, outcome="Yes"):
        return {"source": "polymarket", "id": title, "title": title,
                "outcome": outcome, "probability": 0.5}

    META = "Meta (META) Up or Down on August 19?"
    LADDER = "Will Meta (META) close above ___ end of August?"
    BTC = "BTC price today at 7pm EDT?"

    def test_stock_price_query_finds_the_company_market(self):
        kept = pm_query.filter_relevant([self.market(self.META)], ["Meta stock price"])
        self.assertEqual(len(kept), 1)

    def test_bare_company_name_finds_its_markets(self):
        for title in (self.META, self.LADDER):
            self.assertEqual(len(pm_query.filter_relevant([self.market(title)], ["Meta"])), 1,
                             f"bare entity query should match {title!r}")

    def test_generic_finance_words_are_not_evidence(self):
        """`price` and `above` must not be two of the two required matches."""
        kept = pm_query.filter_relevant([self.market(self.BTC)],
                                        ["Meta Platforms stock price"])
        self.assertEqual(kept, [])

    def test_other_short_entities_work_too(self):
        cases = [("Tesla (TSLA) Up or Down on August 19?", "Tesla stock price"),
                 ("Will Apple close above $300?", "Apple share price"),
                 ("Fed decision in September?", "Fed decision")]
        for title, query in cases:
            self.assertEqual(len(pm_query.filter_relevant([self.market(title)], [query])), 1,
                             f"{query!r} should match {title!r}")


class KeywordGroupIndependenceTest(unittest.TestCase):
    """SKILL.md tells the caller to write two or three keyword groups. The
    gate unioned them and then demanded two matches from the union, so each
    extra phrasing made the filter stricter: one group kept the Up/Down
    market, three groups dropped it."""

    def market(self, title):
        return {"source": "polymarket", "id": title, "title": title,
                "outcome": "Yes", "probability": 0.5}

    def test_extra_groups_never_remove_a_match(self):
        m = self.market("Meta (META) Up or Down on August 19?")
        one = pm_query.filter_relevant([dict(m)], ["Meta up or down"])
        three = pm_query.filter_relevant(
            [dict(m)], ["Meta up or down", "Meta stock price", "Meta share decline"])
        self.assertEqual(len(one), 1)
        self.assertEqual(len(three), 1, "adding phrasings must not tighten the filter")

    def test_a_group_that_matches_nothing_does_not_veto(self):
        m = self.market("US recession by end of 2026?")
        kept = pm_query.filter_relevant([dict(m)], ["US recession 2026", "cat piano lessons"])
        self.assertEqual(len(kept), 1)


class LadderIntegrityTest(unittest.TestCase):
    """"Close above $460" printed 92.4% while "close above $440" printed
    90.0%. A close above 460 is also a close above 440, so one of those is an
    unquoted stub. Six of the thirteen rungs had zero volume."""

    def test_non_monotonic_ladder_is_flagged(self):
        markets = pm_query.parse_polymarket_event(load("poly_event_ladder_nonmonotonic.json"))
        pm_query.check_ladders(markets)
        flagged = [m for m in markets if any("ladder" in f.lower() for f in m["flags"])]
        self.assertTrue(flagged, "non-monotonic rungs should be called out")

    def test_direction_groups_are_checked_separately(self):
        """A touch event mixes ↓ and ↑ rungs. Each side is monotone on its
        own while the combined sequence is not, so checking the event as one
        sequence would fire on every such ladder."""
        markets = []
        for label, p in [("↓ $460", 0.02), ("↓ $480", 0.06), ("↓ $500", 0.14),
                         ("↑ $620", 0.14), ("↑ $640", 0.08), ("↑ $660", 0.06)]:
            markets.append({"source": "polymarket", "title": "hit in August",
                            "outcome": label, "probability": p, "flags": []})
        pm_query.check_ladders(markets)
        self.assertEqual([m["outcome"] for m in markets if m["flags"]], [])

    def test_real_touch_ladder_flags_only_the_broken_side(self):
        """In the captured event the ↓ rungs are clean and the ↑ rungs are
        not: touching $680 prints higher than touching $660."""
        markets = pm_query.parse_polymarket_event(load("poly_event_touch_ladder.json"))
        pm_query.check_ladders(markets)
        down = [m for m in markets if str(m["outcome"]).startswith("↓")]
        up = [m for m in markets if str(m["outcome"]).startswith("↑")]
        self.assertTrue(down and up, "fixture should carry both directions")
        self.assertFalse([m for m in down if any("ladder" in f.lower() for f in m["flags"])],
                         "clean side must not be flagged")
        self.assertTrue([m for m in up if any("ladder" in f.lower() for f in m["flags"])],
                        "broken side must be flagged")

    def test_short_groups_are_left_alone(self):
        markets = [{"source": "polymarket", "title": "t", "outcome": "$100",
                    "probability": 0.9, "flags": []},
                   {"source": "polymarket", "title": "t", "outcome": "$200",
                    "probability": 0.95, "flags": []}]
        pm_query.check_ladders(markets)
        self.assertTrue(all(not m["flags"] for m in markets))


class SpotPriceTest(unittest.TestCase):
    """"52% chance it touches $520" is uninterpretable without knowing the
    stock is at $543.67. The skill had no way to say where the price is."""

    def test_parses_a_quote(self):
        quote = pm_query.parse_spot(load("yahoo_quote_meta.json"))
        self.assertEqual(quote["symbol"], "META")
        self.assertAlmostEqual(quote["price"], 543.67, places=2)
        self.assertIsNotNone(quote["fifty_two_week_low"])
        self.assertIn("Yahoo", quote["source"])


class FetchedAtTest(unittest.TestCase):
    """The mandated output format requires a data timestamp, and step 4 says
    to run detail before writing the answer — but only search returned one."""

    def test_detail_payload_carries_a_timestamp(self):
        market = pm_query.blank_market("polymarket")
        pm_query.stamp(market)
        self.assertIn("fetched_at", market)


class RankingTest(unittest.TestCase):
    """Ranking by volume alone put "Meta headcount this year" above the Meta
    price ladders for a stock-price question: both mention Meta, but only one
    matches what was asked, and the wrong one trades more."""

    def market(self, title, matched, volume):
        return {"source": "kalshi", "id": title, "title": title, "outcome": "Yes",
                "probability": 0.5, "matched": matched, "volume_usd": volume,
                "volume_24h_usd": None, "volume_7d_usd": None}

    def test_more_matched_words_outrank_more_volume(self):
        headcount = self.market("Meta headcount this year", ["meta"], 5_000_000.0)
        ladder = self.market("What will Meta Platforms (META) hit in August?",
                             ["meta", "platform"], 2_000.0)
        ranked = sorted([headcount, ladder], key=pm_query.rank_key)
        self.assertEqual(ranked[0]["title"], ladder["title"])

    def test_volume_still_breaks_ties(self):
        thin = self.market("A", ["meta", "platform"], 1_000.0)
        deep = self.market("B", ["meta", "platform"], 9_000_000.0)
        ranked = sorted([thin, deep], key=pm_query.rank_key)
        self.assertEqual(ranked[0]["title"], "B")


class SingleWordLeakTest(unittest.TestCase):
    """Rescuing one-word matches on word length let any proper noun match
    anything containing it: "will my daughter get into Stanford" matched a
    Stanford football game, and "will I get promoted" matched a GiveWell
    grant for breastfeeding promotion.

    The good one-word matches ("Meta", "recession") are the ones where that
    word IS the whole question. The bad ones leave the half of the question
    that makes it personal unmatched.
    """

    def market(self, title):
        return {"source": "polymarket", "id": title, "title": title,
                "outcome": "Yes", "probability": 0.5}

    def test_proper_noun_alone_does_not_match_an_unrelated_event(self):
        kept = pm_query.filter_relevant([self.market("Hawai'i vs Stanford")],
                                        ["will my daughter get into Stanford"])
        self.assertEqual(kept, [])

    def test_a_different_sense_of_the_same_word_does_not_match(self):
        kept = pm_query.filter_relevant(
            [self.market("Will GiveWell recommend a grant to support breastfeeding promotion?")],
            ["will I get promoted next year at my job"])
        self.assertEqual(kept, [])

    def test_generic_industry_market_does_not_answer_a_personal_one(self):
        kept = pm_query.filter_relevant(
            [self.market("Which companies will be acquired this year?")],
            ["will my company be acquired next year"])
        self.assertEqual(kept, [])

    def test_one_word_questions_still_match_on_that_word(self):
        for title, query in [("Recession this year?", "US recession 2026"),
                             ("Meta (META) Up or Down on August 19?", "Meta stock price"),
                             ("Will Bitcoin close above 150,000?", "Bitcoin price 150k")]:
            self.assertEqual(len(pm_query.filter_relevant([self.market(title)], [query])), 1,
                             f"{query!r} should still match {title!r}")

    def test_amounts_and_years_are_not_content_words(self):
        """`150k` and `2026` discriminate nothing and would otherwise be the
        second match that lets an unrelated market through."""
        for token in ("150k", "2026", "25bps"):
            self.assertNotIn(token, pm_query.content_tokens(f"price {token}"))


class NestedWindowTest(unittest.TestCase):
    """The week of 17 August sits inside August, so a level touched during
    the week is necessarily touched during the month. Polymarket priced
    "touch $540 this week" at 95.5% and "touch $540 in August" at 87.5% —
    an eight-point arbitrage violation that the per-event ladder check
    cannot see, because the two rungs live in different events.
    """

    def markets(self):
        raw = load("poly_search_nested_windows.json")
        out = []
        for event in raw["events"]:
            out.extend(pm_query.parse_polymarket_event(event))
        return out

    def test_shorter_window_pricing_above_longer_is_flagged(self):
        markets = self.markets()
        pm_query.check_windows(markets)
        flagged = [m for m in markets if any("window" in f.lower() for f in m["flags"])]
        self.assertTrue(flagged, "nested-window violation should be called out")
        self.assertTrue(any("540" in str(m["outcome"]) for m in flagged),
                        f"expected the $540 rungs: {[m['outcome'] for m in flagged]}")

    def test_consistent_nested_windows_are_silent(self):
        markets = self.markets()
        pm_query.check_windows(markets)
        consistent = [m for m in markets
                      if "460" in str(m["outcome"]) or "700" in str(m["outcome"])]
        self.assertTrue(consistent)
        for m in consistent:
            self.assertFalse([f for f in m["flags"] if "window" in f.lower()],
                             f"false alarm on {m['outcome']}")

    def test_different_underlyings_are_never_compared(self):
        """A Tesla ladder and a Meta ladder can both have a $520 rung."""
        def rung(title, end, p):
            m = pm_query.blank_market("polymarket")
            m.update({"id": title + end, "title": title, "event_title": title,
                      "outcome": "↓ $520", "probability": p, "end_date": end,
                      "rules": "resolves if the price is reached at any point"})
            return m
        markets = [rung("What will Tesla, Inc. (TSLA) hit Week of August 17?", "2026-08-21T00:00:00Z", 0.95),
                   rung("What will Meta Platforms, Inc. (META) hit in August 2026?", "2026-09-01T00:00:00Z", 0.80)]
        pm_query.check_windows(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "window" in f.lower()], [])

    def test_close_style_ladders_are_not_compared_across_windows(self):
        """Closing above a level on the 21st and on the 31st are unrelated
        questions — neither nests inside the other."""
        def rung(title, end, p):
            m = pm_query.blank_market("polymarket")
            m.update({"id": title + end, "title": title, "event_title": title,
                      "outcome": "$540", "probability": p, "end_date": end,
                      "rules": "resolves to Yes if the closing price is above the level"})
            return m
        markets = [rung("Will Meta (META) finish week of August 17 above___?", "2026-08-21T00:00:00Z", 0.9),
                   rung("Will Meta (META) close above ___ end of August?", "2026-09-01T00:00:00Z", 0.6)]
        pm_query.check_windows(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "window" in f.lower()], [])


class MissingRulesTest(unittest.TestCase):
    """Step 3 of the workflow is "read the resolution criteria". Manifold
    returns none at all, so the verification the skill leans on cannot be
    performed and the output has to say so."""

    def test_market_without_resolution_text_is_flagged(self):
        markets = pm_query.parse_manifold_search(load("manifold_search.json"))
        for m in markets:
            if not m.get("rules"):
                self.assertTrue(any("resolution text" in f.lower() for f in m["flags"]),
                                f"unflagged unverifiable market: {m['title']}")


class DisplayTitleTest(unittest.TestCase):
    """In search output every rung of an event shares one title — "What will
    META hit in August 2026?" — and the direction lives only in `outcome`.
    Quoting title plus probability publishes a number with no idea whether it
    means up or down."""

    def test_rungs_carry_a_self_describing_title(self):
        markets = pm_query.parse_polymarket_event(load("poly_event_touch_ladder.json"))
        down = next(m for m in markets if str(m["outcome"]).startswith("↓"))
        self.assertIn(str(down["outcome"]), down["display_title"])
        self.assertIn("META", down["display_title"])


class SearchContractTest(unittest.TestCase):
    def test_success_has_an_explicit_verdict(self):
        payload = pm_query.build_search_payload(
            keywords=["x"], sources=["polymarket"], errors={},
            markets=[{"source": "polymarket", "id": "a", "probability": 0.5}])
        self.assertEqual(payload["verdict"], "found")

    def test_empty_result_keeps_the_no_market_verdict(self):
        payload = pm_query.build_search_payload(
            keywords=["x"], sources=["polymarket"], errors={}, markets=[])
        self.assertEqual(payload["verdict"], "no_live_market")


# ==========================================================================
# Second regression round: defects found testing the midterms and Bitcoin
# questions (2026-08-18).
# ==========================================================================


class SourceSelectionTest(unittest.TestCase):
    """SKILL.md said "opt in with --sources metaculus". Because --sources
    replaces the list rather than adding to it, that documented action
    queried only Metaculus, found it uninstalled, and returned
    no_live_market — on a question backed by multi-million-dollar markets.
    A documented action must never manufacture a false negative."""

    def test_a_source_that_could_not_run_is_not_reported_as_no_market(self):
        payload = pm_query.build_search_payload(
            keywords=["senate"], sources=["metaculus"],
            errors={"metaculus": "unavailable: scrapling not installed"}, markets=[])
        self.assertNotEqual(payload["verdict"], "no_live_market")
        self.assertEqual(payload["verdict"], "sources_unavailable")

    def test_empty_result_from_working_sources_is_still_no_market(self):
        payload = pm_query.build_search_payload(
            keywords=["x"], sources=["polymarket", "kalshi"], errors={}, markets=[])
        self.assertEqual(payload["verdict"], "no_live_market")

    def test_metaculus_reports_itself_unavailable_rather_than_empty(self):
        if pm_query.metaculus_available():
            self.skipTest("scrapling is installed here")
        with self.assertRaises(RuntimeError):
            pm_query.search_metaculus("anything")


class KalshiVolumeDenominationTest(unittest.TestCase):
    """The v1 search endpoint reports both sides of every trade: its nested
    `volume` is exactly twice the v2 `volume_fp` for the same market, and the
    entry's own `total_volume` agrees with the halved figure. Since the
    thin-market threshold keys on this field, one subcommand can call a
    market noise while the other calls it credible."""

    def test_search_volume_matches_the_venue_total(self):
        raw = load("kalshi_search.json")
        entry = raw["current_page"][0]
        nested = float(entry["markets"][0]["volume"])
        total = float(entry["total_volume"])
        self.assertAlmostEqual(nested / 2, total, delta=1.0,
                               msg="fixture should still show the doubling")
        parsed = next(m for m in pm_query.parse_kalshi_search(raw)
                      if m["id"] == entry["markets"][0]["ticker"])
        self.assertAlmostEqual(parsed["volume_usd"], total, delta=1.0)


class ExactCountLadderTest(unittest.TestCase):
    """"Democrats hold exactly 45 / 46 / 47 seats" is a distribution and is
    supposed to be hump-shaped. Flagging it as inconsistent produces a
    guaranteed false warning, and the skill requires every warning to be
    relayed to the user."""

    def rungs(self, shape_title, labels_and_probs):
        return [{"source": "kalshi", "title": shape_title, "event_title": shape_title,
                 "outcome": label, "probability": p, "flags": []}
                for label, p in labels_and_probs]

    def test_seat_distribution_is_not_called_inconsistent(self):
        markets = self.rungs("How many Senate seats will Democrats hold?",
                             [("45", 0.019), ("46", 0.031), ("47", 0.068),
                              ("48", 0.11), ("49", 0.13), ("50", 0.15), ("51", 0.16)])
        pm_query.check_ladders(markets)
        self.assertEqual([f for m in markets for f in m["flags"]
                          if "inconsistent" in f.lower()], [])

    def test_a_complete_distribution_is_silent(self):
        markets = self.rungs("How many Senate seats will Democrats hold?",
                             [("48", 0.25), ("49", 0.25), ("50", 0.25), ("51", 0.26)])
        pm_query.check_ladders(markets)
        pm_query.check_distribution(markets)
        self.assertEqual([f for m in markets for f in m["flags"]], [])

    def test_a_partial_distribution_says_so(self):
        """Buckets that sum to two thirds look exactly like a complete set."""
        markets = self.rungs("How many Senate seats will Democrats hold?",
                             [("45", 0.019), ("46", 0.031), ("47", 0.068), ("48", 0.11)])
        pm_query.check_distribution(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "incomplete" in f.lower()])

    def test_threshold_ladder_without_arrows_is_still_checked(self):
        """"close above ___" rungs are bare dollar labels but are thresholds."""
        markets = self.rungs("Will Meta (META) close above ___ end of August?",
                             [("$440", 0.8995), ("$460", 0.924), ("$480", 0.8995)])
        pm_query.check_ladders(markets)
        self.assertTrue([f for m in markets for f in m["flags"]])


class AllInversionsTest(unittest.TestCase):
    """The Kalshi Bitcoin ladder had two inversions of identical size. Only
    the first was named, and the one that went unnamed — P(>150k) above
    P(>140k) — was the rung the question was about."""

    def ladder(self, pairs):
        return [{"source": "kalshi", "title": "Highest Bitcoin price this year?",
                 "event_title": "Highest Bitcoin price this year?",
                 "outcome": f"Above ${v}", "probability": p, "flags": []}
                for v, p in pairs]

    def test_every_inversion_is_named(self):
        markets = self.ladder([(99999, 0.11), (109999, 0.06), (119999, 0.07),
                               (129999, 0.06), (139999, 0.03), (149999, 0.04)])
        pm_query.check_ladders(markets)
        note = " ".join(markets[0]["flags"])
        self.assertIn("119999", note)
        self.assertIn("149999", note)

    def test_a_wholly_inverted_ladder_is_flagged(self):
        """Requiring both an up-step and a down-step let a ladder that only
        ever rises pass unchallenged."""
        markets = self.ladder([(100000, 0.02), (150000, 0.05), (200000, 0.09)])
        pm_query.check_ladders(markets)
        self.assertTrue([f for m in markets for f in m["flags"]])


class KalshiTemplateRulesTest(unittest.TestCase):
    """One Kalshi market returned its rules as an unrendered template —
    "above || Count || by || Date || at || Time ||" — which makes the
    resolution check the workflow depends on impossible."""

    def test_unrendered_template_is_treated_as_missing(self):
        raw = {"markets": [{"ticker": "X", "title": "t", "status": "active",
                            "last_price_dollars": "0.03", "volume_fp": "100000",
                            "rules_primary": "If the price is above || Count || by "
                                             "|| Date || at || Time ||, then Yes."}]}
        market = pm_query.parse_kalshi_v2_markets(raw)[0]
        self.assertIsNone(market["rules"])
        self.assertTrue(any("resolution text" in f.lower() for f in market["flags"]))


class HistoryByDateTest(unittest.TestCase):
    """Deltas were taken by list index, but the series can carry two points
    for the same day, which silently shifts what "seven days ago" means."""

    def test_duplicate_days_do_not_shift_the_window(self):
        history = [{"date": "2026-08-11", "p": 0.40}, {"date": "2026-08-12", "p": 0.41},
                   {"date": "2026-08-13", "p": 0.42}, {"date": "2026-08-14", "p": 0.43},
                   {"date": "2026-08-15", "p": 0.44}, {"date": "2026-08-16", "p": 0.45},
                   {"date": "2026-08-17", "p": 0.46}, {"date": "2026-08-18", "p": 0.14},
                   {"date": "2026-08-18", "p": 0.50}]
        day, week = pm_query.changes_from_history(history)
        self.assertAlmostEqual(week, 0.10, places=4)   # 0.50 vs 08-11's 0.40
        self.assertAlmostEqual(day, 0.04, places=4)    # 0.50 vs 08-17's 0.46


class QueryNumberRungTest(unittest.TestCase):
    """A 32-rung ladder was capped at 20 by volume, and the rung dropped was
    the 150,000 one — in a search whose keyword group was "Bitcoin 150k"."""

    def test_numbers_in_the_question_are_recognised(self):
        self.assertEqual(pm_query.keyword_numbers(["Bitcoin 150k", "BTC"]), {150000.0})
        self.assertEqual(pm_query.keyword_numbers(["META 520"]), {520.0})
        self.assertIn(1500000.0, pm_query.keyword_numbers(["1.5m market cap"]))


class CrossEventThresholdTest(unittest.TestCase):
    """On one venue, "hit $170k in 2026" printed 2.15% while the standalone
    "hit $150k by Dec 31 2026" printed 1.25%. Touching 170k means touching
    150k first, so that ordering is impossible — and the two rungs live in
    different events, where the per-event check cannot see them."""

    def touch(self, title, outcome, p, end="2026-12-31T00:00:00Z"):
        m = pm_query.blank_market("polymarket")
        m.update({"id": outcome + title, "title": title, "event_title": title,
                  "outcome": outcome, "probability": p, "end_date": end,
                  "rules": "resolves Yes if the price is reached at any point"})
        return m

    def test_same_underlying_and_deadline_must_stay_ordered(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ $170,000", 0.0215),
                   self.touch("Will Bitcoin hit $150k by December 31, 2026?", "↑ $150,000", 0.0125)]
        pm_query.check_cross_event_thresholds(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "cross-market" in f.lower()])

    def test_consistent_pair_is_silent(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ $170,000", 0.01),
                   self.touch("Will Bitcoin hit $150k by December 31, 2026?", "↑ $150,000", 0.03)]
        pm_query.check_cross_event_thresholds(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "cross-market" in f.lower()], [])

    def test_different_deadlines_are_not_compared(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ $170,000", 0.0215,
                              end="2026-12-31T00:00:00Z"),
                   self.touch("Will Bitcoin hit $150k by March 2026?", "↑ $150,000", 0.0125,
                              end="2026-03-31T00:00:00Z")]
        pm_query.check_cross_event_thresholds(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "cross-market" in f.lower()], [])


class TickerAliasTest(unittest.TestCase):
    """Searching "Bitcoin" threw away every market titled "BTC ...", and on a
    thinner coin that gap is the difference between an answer and a wrongly
    confident "no market"."""

    def market(self, title):
        return {"source": "polymarket", "id": title, "title": title,
                "outcome": "Yes", "probability": 0.5}

    def test_bitcoin_matches_btc_titles(self):
        kept = pm_query.filter_relevant(
            [self.market("Will BTC hit $50,000 before $100,000?")], ["Bitcoin"])
        self.assertEqual(len(kept), 1)

    def test_btc_matches_bitcoin_titles(self):
        kept = pm_query.filter_relevant(
            [self.market("Will Bitcoin close above 100k this year?")], ["BTC price"])
        self.assertEqual(len(kept), 1)

    def test_aliases_do_not_collapse_different_coins(self):
        kept = pm_query.filter_relevant(
            [self.market("Will Ethereum close above 5000?")], ["Bitcoin"])
        self.assertEqual(kept, [])


class CompareOutputTest(unittest.TestCase):
    """`compare` returned two detail payloads and nothing else, while the
    output template demands a cross-check line — so the comparison the
    subcommand exists for stayed with the model."""

    def test_reports_the_spread_and_whether_they_agree(self):
        rules = "resolves Yes if the party completes a Senate majority"
        summary = pm_query.compare_summary([
            {"source": "polymarket", "probability": 0.515, "rules": rules,
             "end_date": "2026-11-03T00:00:00Z"},
            {"source": "kalshi", "probability": 0.49, "rules": rules,
             "end_date": "2026-11-03T00:00:00Z"},
        ])
        self.assertAlmostEqual(summary["spread_pp"], 2.5, places=1)
        self.assertTrue(summary["agree"])

    def test_refuses_to_claim_agreement_it_could_not_verify(self):
        """Two markets three points apart were called agreement when one
        resolved on a company confirming an IPO and the other on completing
        one — and the deadline check had silently skipped, because one side
        reported no end date at all."""
        summary = pm_query.compare_summary([
            {"source": "polymarket", "probability": 0.22, "end_date": None,
             "rules": "resolves Yes if OpenAI completes an Initial Public Offering"},
            {"source": "kalshi", "probability": 0.25, "end_date": "2027-01-01T00:00:00Z",
             "rules": "resolves Yes if OpenAI confirms an IPO before Jan 1, 2027"},
        ])
        self.assertIsNone(summary["agree"])
        self.assertTrue([c for c in summary["caveats"] if "resolution differs" in c.lower()])
        self.assertTrue([c for c in summary["caveats"] if "end date" in c.lower()])

    def test_flags_a_wide_gap(self):
        summary = pm_query.compare_summary([
            {"source": "polymarket", "probability": 0.73, "end_date": None},
            {"source": "kalshi", "probability": 0.62, "end_date": None},
        ])
        self.assertFalse(summary["agree"])

    def test_warns_when_the_deadlines_differ(self):
        summary = pm_query.compare_summary([
            {"source": "polymarket", "probability": 0.515, "end_date": "2026-11-03T00:00:00Z"},
            {"source": "kalshi", "probability": 0.49, "end_date": "2027-02-01T00:00:00Z"},
        ])
        self.assertTrue([c for c in summary["caveats"] if "deadline" in c.lower()])


class LongShotWordingTest(unittest.TestCase):
    """"Market treats this as all but settled" fired on every tail rung of an
    18-rung ladder. Relaying that verbatim tells the reader their market is
    over, which is false — it is a long shot, not a settled question."""

    def test_tail_price_is_called_a_long_shot(self):
        flags = pm_query.credibility_flags(
            {"source": "polymarket", "probability": 0.011, "volume_usd": 3_000_000.0,
             "volume_24h_usd": 25_000.0, "liquidity_usd": 200_000.0})
        self.assertTrue(any("long shot" in f.lower() for f in flags))
        self.assertFalse(any("settled" in f.lower() for f in flags))


# ==========================================================================
# Third round: a "who wins" field with named outcomes — the shape none of
# the price-ladder and binary tests had exercised (2026-08-18).
# ==========================================================================


class NamedOutcomeOrderTest(unittest.TestCase):
    """Within one event every candidate matched the same words, so lifetime
    volume alone decided the order: the 8.5% favourite printed last and a
    2.45% long shot printed first. Following "list the top three outcomes"
    against that order names the wrong three people."""

    def field(self):
        rows = [("Donald Trump", 0.0245, 4_378_740.0), ("UNRWA", 0.0425, 2_044_656.0),
                ("Greta Thunberg", 0.011, 1_468_717.0), ("Yulia Navalnaya", 0.085, 290_687.0)]
        out = []
        for name, p, vol in rows:
            m = pm_query.blank_market("polymarket")
            m.update({"id": name, "title": "Nobel Peace Prize Winner 2026",
                      "event_title": "Nobel Peace Prize Winner 2026", "outcome": name,
                      "probability": p, "volume_usd": vol})
            out.append(m)
        return out

    def test_named_field_is_ordered_by_probability(self):
        ordered = pm_query.order_rungs(self.field())
        self.assertEqual(ordered[0]["outcome"], "Yulia Navalnaya")
        self.assertEqual([m["outcome"] for m in ordered][:2], ["Yulia Navalnaya", "UNRWA"])

    def test_threshold_ladders_keep_threshold_order(self):
        rungs = []
        for label, p in [("↓ $540", 0.875), ("↓ $460", 0.02), ("↓ $500", 0.14)]:
            m = pm_query.blank_market("polymarket")
            m.update({"id": label, "title": "hit in August", "event_title": "hit in August",
                      "outcome": label, "probability": p, "volume_usd": 1000.0})
            rungs.append(m)
        ordered = pm_query.order_rungs(rungs)
        self.assertEqual([m["outcome"] for m in ordered], ["↓ $460", "↓ $500", "↓ $540"])


class DistributionSumTest(unittest.TestCase):
    """Twenty Polymarket candidates summed to 36% — two thirds of the
    probability sat on names never returned — and eighteen Kalshi candidates
    summed to 119%, which no set of coherent prices can do. Neither was
    mentioned, because the sum check only ran on labels containing a digit."""

    def field(self, probs, source="polymarket"):
        out = []
        for i, p in enumerate(probs):
            m = pm_query.blank_market(source)
            m.update({"id": f"c{i}", "title": "Nobel Peace Prize Winner 2026",
                      "event_title": "Nobel Peace Prize Winner 2026",
                      "outcome": f"Candidate {chr(65 + i)}", "probability": p})
            out.append(m)
        return out

    def test_named_field_missing_most_of_its_mass_is_flagged(self):
        markets = self.field([0.085, 0.054, 0.0425, 0.0245, 0.011])
        pm_query.check_distribution(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "incomplete" in f.lower()])

    def test_named_field_summing_over_one_is_flagged(self):
        markets = self.field([0.30, 0.15, 0.12, 0.32, 0.30])
        pm_query.check_distribution(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "incoheren" in f.lower()])

    def test_a_coherent_field_is_silent(self):
        markets = self.field([0.4, 0.3, 0.2, 0.08])
        pm_query.check_distribution(markets)
        self.assertEqual([f for m in markets for f in m["flags"]], [])

    def test_touch_ladders_are_exempt(self):
        """Touching 540 and touching 520 are not alternatives, so those
        probabilities are supposed to sum past 100%."""
        markets = []
        for label, p in [("↓ $540", 0.875), ("↓ $520", 0.505), ("↓ $500", 0.14), ("↓ $480", 0.05)]:
            m = pm_query.blank_market("polymarket")
            m.update({"id": label, "title": "hit in August", "event_title": "hit in August",
                      "outcome": label, "probability": p})
            markets.append(m)
        pm_query.check_distribution(markets)
        self.assertEqual([f for m in markets for f in m["flags"]], [])


class UniversalDisplayTitleTest(unittest.TestCase):
    """SKILL.md says to quote display_title. Kalshi and Manifold candidates
    did not have one, and their `title` is the event name repeated on every
    row — "Nobel Peace Prize winner — 30%" with no candidate named."""

    def test_kalshi_candidates_are_self_describing(self):
        markets = pm_query.parse_kalshi_v2_markets(load("kalshi_markets_new_fields.json"))
        for m in markets:
            self.assertTrue(m.get("display_title"))

    def test_manifold_candidates_are_self_describing(self):
        for m in pm_query.parse_manifold_search(load("manifold_search.json")):
            self.assertTrue(m.get("display_title"))


# ==========================================================================
# Fourth round: defects found re-testing the Bitcoin ladder (2026-08-18).
# ==========================================================================


class SpotSymbolTest(unittest.TestCase):
    """`spot BTC` returns $28.57 — Grayscale's Bitcoin Mini Trust ETF — while
    Bitcoin trades at $64,685. SKILL.md's only example was `spot META`, so
    following it anchors a $150k ladder to a $28 share price with no warning.
    The anchor step exists precisely to stop the ladder being misread."""

    def test_bare_crypto_tickers_resolve_to_the_coin(self):
        for symbol in ("BTC", "btc", "ETH", "SOL", "DOGE"):
            self.assertTrue(pm_query.resolve_symbol(symbol).endswith("-USD"),
                            f"{symbol} should resolve to the coin, not an ETF")

    def test_equities_are_left_alone(self):
        for symbol in ("META", "TSLA", "AAPL", "SPY"):
            self.assertEqual(pm_query.resolve_symbol(symbol), symbol.upper())

    def test_an_explicit_pair_is_respected(self):
        self.assertEqual(pm_query.resolve_symbol("BTC-USD"), "BTC-USD")

    def test_quote_names_the_instrument_it_priced(self):
        quote = pm_query.parse_spot(load("yahoo_quote_meta.json"))
        self.assertIn("name", quote)
        self.assertIn("instrument_type", quote)


class SuffixedThresholdTest(unittest.TestCase):
    """"1M+" parsed as the number 1, so a MicroStrategy market about how many
    coins it holds was compared against a Bitcoin price ladder and produced a
    cross-market warning — which SKILL.md then requires be shown to the user."""

    def test_magnitude_suffixes_are_scaled(self):
        self.assertEqual(pm_query._ladder_parts({"outcome": "1M+"})[1], 1_000_000.0)
        self.assertEqual(pm_query._ladder_parts({"outcome": "↑ 150k"})[1], 150_000.0)

    def test_plain_numbers_are_unaffected(self):
        self.assertEqual(pm_query._ladder_parts({"outcome": "↑ 90,000"})[1], 90_000.0)


class UnderlyingIdentityTest(unittest.TestCase):
    """A market about MicroStrategy's coin holdings shares the word "bitcoin"
    with a Bitcoin price ladder, and one shared word was being taken as
    "same underlying"."""

    def test_a_company_holding_the_asset_is_not_the_asset(self):
        a = ("tokens", frozenset({"bitcoin"}))
        b = ("tokens", frozenset({"microstrategy", "announc", "hold", "bitcoin"}))
        self.assertFalse(pm_query._same_underlying(a, b))

    def test_the_same_subject_still_matches(self):
        a = ("tokens", frozenset({"bitcoin"}))
        b = ("tokens", frozenset({"bitcoin"}))
        self.assertTrue(pm_query._same_underlying(a, b))

    def test_one_extra_qualifier_is_tolerated(self):
        a = ("tokens", frozenset({"bitcoin"}))
        b = ("tokens", frozenset({"bitcoin", "spot"}))
        self.assertTrue(pm_query._same_underlying(a, b))


class SameLevelAcrossEventsTest(unittest.TestCase):
    """Two Polymarket markets, same level, same deadline: the ladder rung
    ↑150,000 printed 2.5% while the standalone "hit $150k by December 31,
    2026" printed 1.25%. Both over a million dollars of volume, twice apart,
    and nothing said so — the standalone market keeps its level in the title,
    where no check was looking."""

    def touch(self, title, outcome, p):
        m = pm_query.blank_market("polymarket")
        m.update({"id": title + outcome, "title": title, "event_title": title,
                  "event_id": title, "outcome": outcome, "probability": p,
                  "end_date": "2026-12-31T00:00:00Z",
                  "rules": "resolves Yes if reached at any point"})
        return m

    def test_same_level_priced_twice_apart_is_flagged(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ 150,000", 0.025),
                   self.touch("Will Bitcoin hit $150k by December 31, 2026?",
                              "by December 31, 2026", 0.0125)]
        pm_query.check_same_level(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "same level" in f.lower()])

    def test_close_prices_on_the_same_level_are_silent(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ 150,000", 0.025),
                   self.touch("Will Bitcoin hit $150k by December 31, 2026?",
                              "by December 31, 2026", 0.024)]
        pm_query.check_same_level(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "same level" in f.lower()], [])

    def test_different_levels_are_not_compared(self):
        markets = [self.touch("What price will Bitcoin hit in 2026?", "↑ 170,000", 0.0215),
                   self.touch("Will Bitcoin hit $150k by December 31, 2026?",
                              "by December 31, 2026", 0.0125)]
        pm_query.check_same_level(markets)
        self.assertEqual([f for m in markets for f in m["flags"] if "same level" in f.lower()], [])


class DateIsNotALevelTest(unittest.TestCase):
    """"by December 31, 2026" parsed as the threshold 31, so a market whose
    level lives in its title was compared against a price ladder as though
    it were a $31 rung — producing eight cross-market warnings at once, all
    of which SKILL.md would force into the answer."""

    def market(self, title, outcome, p):
        m = pm_query.blank_market("polymarket")
        m.update({"id": title + outcome, "title": title, "event_title": title,
                  "event_id": title, "outcome": outcome, "probability": p,
                  "end_date": "2026-12-31T00:00:00Z",
                  "rules": "resolves Yes if reached at any point"})
        return m

    def test_a_date_label_is_not_treated_as_a_threshold(self):
        markets = [self.market("What price will Bitcoin hit in 2026?", "↑ 90,000", 0.125),
                   self.market("When will Bitcoin hit $150k?", "by December 31, 2026", 0.012)]
        pm_query.check_cross_event_thresholds(markets)
        self.assertEqual([f for m in markets for f in m["flags"]
                          if "cross-market" in f.lower()], [],
                         "a date must not be compared against a price rung")

    def test_a_genuine_cross_event_inversion_still_fires(self):
        markets = [self.market("What price will Bitcoin hit in 2026?", "↑ 200,000", 0.018),
                   self.market("When will Bitcoin hit $150k?", "by December 31, 2026", 0.012)]
        pm_query.check_cross_event_thresholds(markets)
        self.assertTrue([f for m in markets for f in m["flags"] if "cross-market" in f.lower()],
                        "touching 200k cannot be likelier than touching 150k")
