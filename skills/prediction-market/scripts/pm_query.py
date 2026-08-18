#!/usr/bin/env python3
"""Query real-money prediction markets and return normalized, compact JSON.

Stdlib only, so it runs unchanged in Claude Code and in the claude.ai sandbox.

    pm_query.py search "fed rate cut" "fomc september"
    pm_query.py detail polymarket 0xc600...5e93
    pm_query.py detail kalshi KXFEDDECISION-26SEP-H0
    pm_query.py detail manifold <marketId>

Design notes live in references/sources.md. The short version: provider
responses are full of already-settled markets, and several fields that look
authoritative are not. Everything here filters and labels accordingly.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

TIMEOUT = 8
UA = "Mozilla/5.0 (compatible; prediction-market-skill/1.0)"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
POLY_DATA = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com"
MANIFOLD = "https://api.manifold.markets/v0"

REAL_MONEY = ("polymarket", "kalshi")

# Credibility thresholds (USD). Below these a quoted probability is noise.
MIN_VOLUME = 50_000
MIN_LIQUIDITY = 5_000
NEAR_CERTAIN = 0.02
DIVERGENCE = 0.05


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_json(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def blank_market(source):
    return {
        "source": source,
        "id": None,
        "title": None,
        "outcome": None,
        "probability": None,
        "probability_of": None,
        "outcome_prices": [],
        "prob_24h_change": None,
        "prob_7d_change": None,
        "volume_usd": None,
        "volume_24h_usd": None,
        "volume_7d_usd": None,
        "event_id": None,
        "event_volume_24h_usd": None,
        "liquidity_usd": None,
        "open_interest_usd": None,
        "participants": None,
        "participants_label": None,
        "participants_truncated": False,
        "end_date": None,
        "end_date_passed": False,
        "url": None,
        "rules": None,
        "flags": [],
    }


# --------------------------------------------------------------------------
# Credibility
# --------------------------------------------------------------------------

def stamp(market):
    """Every payload the model quotes needs a data time — the answer format
    requires one and `detail` is the call it is told to quote from."""
    market["fetched_at"] = now_utc().isoformat(timespec="seconds")
    return market


def label_outcomes(market, labels, prices):
    """Attach which outcome each price belongs to.

    Without this a caller sees `probability: 0.555` on a market whose
    outcomes are ["Up", "Down"] and has no way to tell that 0.555 is *Up*.
    On a question phrased "will it fall" that reads as the opposite of the
    market's view.
    """
    labels = [str(x) for x in (labels or [])]
    pairs = [{"label": l, "p": p} for l, p in zip(labels, prices or []) if p is not None]
    market["outcome_prices"] = pairs
    market["probability_of"] = pairs[0]["label"] if pairs else None
    return market


def credibility_flags(market):
    """Warnings a reader needs before trusting the number. Order matters:
    most disqualifying first."""
    source = market.get("source")
    flags = []

    if source == "manifold":
        # Manifold ships no resolution text at all, so the workflow's
        # verification step cannot be performed here — unlike Kalshi, where
        # search omits the rules but `detail` supplies them.
        if not market.get("rules"):
            flags.append("⚠️ No resolution text available — cannot verify what "
                         "this market resolves on")
        flags.append("⚠️ Play-money market — indicative only")
        bettors = market.get("participants")
        if bettors is not None and bettors < 20:
            flags.append(f"⚠️ Only {bettors} bettors — mostly noise")
        return flags

    if source == "metaculus":
        n = market.get("participants")
        flags.append(f"Not a market — forecaster consensus ({n} forecasters)"
                     if n else "Not a market — forecaster consensus")
        return flags

    volume = market.get("volume_usd")
    if volume is not None and volume < MIN_VOLUME:
        flags.append(f"⚠️ Thin market (${volume:,.0f} lifetime volume) — this number is noise")

    liquidity = market.get("liquidity_usd")
    open_interest = market.get("open_interest_usd")
    if liquidity is not None and liquidity < MIN_LIQUIDITY:
        flags.append(f"⚠️ Shallow book (${liquidity:,.0f} resting) — one sizeable order moves the price")
    elif liquidity is None and open_interest is not None and open_interest < MIN_LIQUIDITY:
        flags.append(f"⚠️ Only ${open_interest:,.0f} open interest — too thin to lean on")

    recent = (market.get("volume_24h_usd") or market.get("volume_7d_usd")
              or market.get("event_volume_24h_usd"))
    if not recent:
        flags.append("⚠️ No recent trading — the price may be stale")

    if market.get("end_date_passed"):
        flags.append(f"⚠️ End date already passed ({str(market.get('end_date'))[:10]}) "
                     f"— this may be awaiting settlement, not a live view")

    prob = market.get("probability")
    if prob is not None and (prob < NEAR_CERTAIN or prob > 1 - NEAR_CERTAIN):
        flags.append("Priced as a long shot" if prob < 0.5
                     else "Priced as near certain")

    return flags


def divergence_note(markets):
    """Two real-money venues disagreeing is signal. Play money is not."""
    probs = [m["probability"] for m in markets
             if m.get("source") in REAL_MONEY and m.get("probability") is not None]
    if len(probs) < 2:
        return None
    gap = max(probs) - min(probs)
    if gap <= DIVERGENCE:
        return None
    return (f"Real-money venues disagree by {gap * 100:.0f} points "
            f"({min(probs) * 100:.0f}% vs {max(probs) * 100:.0f}%)")


def finalize(market):
    market["flags"] = credibility_flags(market)
    return market


# --------------------------------------------------------------------------
# Polymarket
# --------------------------------------------------------------------------

def _poly_json_list(raw_market, key):
    value = raw_market.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])


def _poly_prices(raw_market):
    return [as_float(p) for p in _poly_json_list(raw_market, "outcomePrices")]


def _poly_labels(raw_market):
    return _poly_json_list(raw_market, "outcomes")


def is_polymarket_resolved(raw_market):
    """A sub-market is dead if it is closed, or if its prices have collapsed
    to exactly 0/1. A live market can legitimately sit at 0.95% — that is a
    market view, not a settlement — so only exact 0/1 counts."""
    if raw_market.get("closed"):
        return True
    prices = _poly_prices(raw_market)
    return bool(prices) and all(p in (0.0, 1.0) for p in prices if p is not None)


def parse_polymarket_event(event):
    """Flatten one Gamma event into live sub-markets.

    Volume is read per sub-market on purpose: the event-level total sums in
    every settled sub-market and badly overstates a live one's credibility.
    """
    out = []
    event_slug = event.get("slug")
    for raw in event.get("markets") or []:
        if is_polymarket_resolved(raw):
            continue
        prices = _poly_prices(raw)
        market = blank_market("polymarket")
        end = raw.get("endDate") or event.get("endDate")
        parsed_end = parse_iso(end)
        market.update({
            "id": raw.get("conditionId"),
            "title": event.get("title"),
            "outcome": raw.get("groupItemTitle") or raw.get("question"),
            "probability": prices[0] if prices else None,
            "prob_24h_change": as_float(raw.get("oneDayPriceChange")),
            "prob_7d_change": as_float(raw.get("oneWeekPriceChange")),
            "volume_usd": as_float(raw.get("volumeNum")) or as_float(raw.get("volume")),
            "volume_24h_usd": as_float(raw.get("volume24hr")),
            "volume_7d_usd": as_float(raw.get("volume1wk")),
            "liquidity_usd": as_float(raw.get("liquidityNum")) or as_float(raw.get("liquidity")),
            "end_date": end,
            "end_date_passed": bool(parsed_end and parsed_end < now_utc()),
            "url": _poly_url(event_slug, raw.get("slug")),
            "rules": raw.get("description") or event.get("description"),
            "event_title": event.get("title"),
            "event_id": str(event.get("id") or event.get("slug") or event.get("title") or ""),
            "clob_token_id": (json.loads(raw["clobTokenIds"])[0]
                              if isinstance(raw.get("clobTokenIds"), str) and raw["clobTokenIds"]
                              else None),
        })
        market["display_title"] = _display_title(market)
        label_outcomes(market, _poly_labels(raw) or ["Yes", "No"], prices)
        out.append(finalize(market))
    return out


def changes_from_history(history):
    """(1-day, 1-week) deltas from the daily price series.

    Taken by list index this silently mis-measures: the series can carry two
    points for the same day, so "seven entries back" stops being "seven days
    ago". Walk by date instead.
    """
    if len(history) < 2:
        return (None, None)
    latest = history[-1]
    last_date = parse_iso(latest["date"])
    if last_date is None:
        return (None, None)

    earlier = [p for p in history[:-1]
               if (parse_iso(p["date"]) or last_date) < last_date]
    if not earlier:
        return (None, None)
    day = round(latest["p"] - earlier[-1]["p"], 4)

    cutoff = last_date - timedelta(days=7)
    week_points = [p for p in earlier if (parse_iso(p["date"]) or last_date) <= cutoff]
    week = round(latest["p"] - week_points[-1]["p"], 4) if week_points else None
    return (day, week)


def _display_title(market):
    """A rung has to be quotable on its own.

    In search output every rung of an event shares the event's title, so
    "What will META hit in August 2026?" plus 50.5% says nothing about
    whether that is a rise or a fall — the direction lives only in the
    outcome label.
    """
    title = str(market.get("title") or "").strip()
    outcome = str(market.get("outcome") or "").strip()
    if not outcome or outcome == title or outcome.lower() in ("yes", "no"):
        return title
    return f"{title} — {outcome}" if title else outcome


def _poly_url(event_slug, market_slug):
    if event_slug and market_slug:
        return f"https://polymarket.com/event/{event_slug}/{market_slug}"
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    return "https://polymarket.com"


def filter_polymarket_search(raw):
    """Search returns a lot of already-settled events; drop them."""
    return [e for e in (raw.get("events") or [])
            if not e.get("closed") and not e.get("archived")]


def search_polymarket(keyword, limit=6):
    url = (f"{GAMMA}/public-search?q={urllib.parse.quote(keyword)}"
           f"&limit_per_type={limit}&events_status=active")
    markets = []
    for event in filter_polymarket_search(http_json(url)):
        markets.extend(parse_polymarket_event(event))
    return markets


def count_holders(raw, limit):
    """Distinct wallets holding either side. A page that comes back full means
    the real number is higher than what we can see."""
    wallets = set()
    truncated = False
    for token in raw or []:
        holders = token.get("holders") or []
        if len(holders) >= limit:
            truncated = True
        for holder in holders:
            wallet = holder.get("proxyWallet")
            if wallet:
                wallets.add(wallet)
    return len(wallets), truncated


def detail_polymarket(condition_id):
    limit = 500
    raw = http_json(f"{GAMMA}/markets?condition_ids={urllib.parse.quote(condition_id)}")
    if not raw:
        raise LookupError(f"no market for conditionId {condition_id}")
    raw_market = raw[0]
    # A mistyped filter param (conditionIds vs condition_ids) is silently
    # ignored by Gamma and returns an unrelated market. Never trust the hit.
    if raw_market.get("conditionId") != condition_id:
        raise LookupError(
            f"Gamma returned {raw_market.get('conditionId')} for {condition_id}")

    event = {"title": raw_market.get("question"),
             "slug": (raw_market.get("events") or [{}])[0].get("slug"),
             "markets": [raw_market]}
    markets = parse_polymarket_event(event)
    if not markets:
        market = blank_market("polymarket")
        market.update({"id": condition_id, "title": raw_market.get("question"),
                       "resolved": True})
        market["flags"] = ["This market has settled — not a current probability"]
        return market

    market = markets[0]
    if not market.get("end_date"):
        market["end_date"] = (raw_market.get("endDate")
                              or (raw_market.get("events") or [{}])[0].get("endDate"))
    try:
        holders_raw = http_json(
            f"{POLY_DATA}/holders?market={urllib.parse.quote(condition_id)}&limit={limit}")
        count, truncated = count_holders(holders_raw, limit)
        market["participants"] = count
        market["participants_label"] = "holders"
        market["participants_truncated"] = truncated
    except Exception as exc:  # holders are a nice-to-have, never fatal
        market["participants_error"] = str(exc)

    token = market.get("clob_token_id")
    if token:
        try:
            hist = http_json(f"{CLOB}/prices-history?market={token}&interval=1m&fidelity=1440")
            market["history"] = [
                {"date": datetime.fromtimestamp(p["t"], timezone.utc).strftime("%Y-%m-%d"),
                 "p": round(p["p"], 4)}
                for p in (hist.get("history") or [])[-30:]
            ]
            day, week = changes_from_history(market["history"])
            if market["prob_24h_change"] is None:
                market["prob_24h_change"] = day
            if market["prob_7d_change"] is None:
                market["prob_7d_change"] = week
        except Exception as exc:
            market["history_error"] = str(exc)

    market["flags"] = credibility_flags(market)
    return stamp(market)


# --------------------------------------------------------------------------
# Kalshi
# --------------------------------------------------------------------------
# Kalshi renamed its money fields. yes_bid / yes_ask / last_price / volume /
# open_interest are gone from v2 responses entirely — reading them yields
# None and a silently empty report. The v1 search endpoint still carries the
# old cent-denominated names alongside the new ones.

def _usable_rules(text):
    """Kalshi sometimes ships the rule template rather than the rule:
    "above || Count || by || Date || at || Time ||". That cannot be verified
    against anything, so treat it as absent rather than as text."""
    if text and "||" in text:
        return None
    return text or None


def _kalshi_url(ticker):
    series = (ticker or "").split("-")[0].lower()
    return f"https://kalshi.com/markets/{series}" if series else "https://kalshi.com"


def parse_kalshi_v2_markets(raw):
    out = []
    for m in raw.get("markets") or []:
        if m.get("status") not in (None, "active", "initialized"):
            continue
        prob = as_float(m.get("last_price_dollars"))
        if prob is None:
            bid, ask = as_float(m.get("yes_bid_dollars")), as_float(m.get("yes_ask_dollars"))
            if bid is not None and ask is not None:
                prob = (bid + ask) / 2
        previous = as_float(m.get("previous_price_dollars"))
        end = m.get("close_time")
        parsed_end = parse_iso(end)
        market = blank_market("kalshi")
        market.update({
            "id": m.get("ticker"),
            "title": m.get("title"),
            "outcome": m.get("yes_sub_title") or m.get("subtitle"),
            "probability": prob,
            "prob_24h_change": round(prob - previous, 4) if (prob is not None and previous is not None) else None,
            "volume_usd": as_float(m.get("volume_fp")),
            "volume_24h_usd": as_float(m.get("volume_24h_fp")),
            # liquidity_dollars comes back as 0.0000 on every Kalshi market,
            # including ones with millions traded. It is not a depth measure;
            # open interest is.
            "liquidity_usd": as_float(m.get("liquidity_dollars")) or None,
            "open_interest_usd": as_float(m.get("open_interest_fp")),
            "participants_label": "open interest (Kalshi publishes no trader count)",
            "end_date": end,
            "event_id": m.get("event_ticker"),
            "end_date_passed": bool(parsed_end and parsed_end < now_utc()),
            "url": _kalshi_url(m.get("ticker")),
            "rules": _usable_rules(m.get("rules_primary")),
        })
        market["display_title"] = _display_title(market)
        label_outcomes(market, ["Yes", "No"],
                       [prob, (1 - prob) if prob is not None else None])
        finalize(market)
        if not market["rules"]:
            # v2 is the endpoint that carries rules; missing here means the
            # venue shipped an unrendered template, and the workflow's
            # resolution check cannot be performed on this market.
            market["flags"].insert(0, "⚠️ No resolution text available — cannot "
                                      "verify what this market resolves on")
        out.append(market)
    return out


def parse_kalshi_search(raw):
    out = []
    for entry in raw.get("current_page") or []:
        if entry.get("type") != "contract":
            continue
        for m in entry.get("markets") or []:
            prob = as_float(m.get("last_price_dollars"))
            previous = as_float(m.get("previous_price_dollars"))
            end = m.get("close_ts")
            parsed_end = parse_iso(end)
            if parsed_end and parsed_end < now_utc():
                continue
            if m.get("result"):
                continue
            ticker = m.get("ticker") or entry.get("event_ticker")
            market = blank_market("kalshi")
            market.update({
                "id": ticker,
                "title": entry.get("event_title") or entry.get("series_title"),
                "outcome": m.get("yes_subtitle") or m.get("title") or "Yes",
                "probability": prob,
                "prob_24h_change": round(prob - previous, 4) if (prob is not None and previous is not None) else None,
                # The v1 search endpoint counts both sides of every trade:
                # its nested `volume` is exactly twice v2's `volume_fp`, and
                # the entry's own `total_volume` agrees with the halved
                # figure. Contracts settle at $1, so halved contracts ≈ USD.
                "volume_usd": (as_float(m.get("volume")) / 2 if as_float(m.get("volume"))
                               else as_float(entry.get("total_volume"))),
                # Venue-level, covering every sibling market — never this
                # market's own 24h figure.
                "event_volume_24h_usd": as_float(entry.get("recent_volume")),
                "participants_label": "open interest (Kalshi publishes no trader count)",
                "end_date": end,
                "event_id": entry.get("event_ticker") or entry.get("series_ticker"),
                "url": _kalshi_url(entry.get("series_ticker") or ticker),
            })
            market["display_title"] = _display_title(market)
            label_outcomes(market, ["Yes", "No"],
                           [prob, (1 - prob) if prob is not None else None])
            out.append(finalize(market))
    return out


def search_kalshi(keyword, limit=6):
    """The v1 search endpoint is undocumented. If it disappears, the caller
    reports Kalshi as unavailable rather than failing the whole query."""
    url = f"{KALSHI}/v1/search/series?query={urllib.parse.quote(keyword)}"
    return parse_kalshi_search(http_json(url))[: limit * 3]


def kalshi_history(ticker, days=30):
    """Daily closes from Kalshi's candlestick endpoint.

    `detail` promised a 30-day trend and delivered it only for Polymarket,
    so the trend line of the answer format was unfillable for the venue with
    the deeper book on many questions.
    """
    series = (ticker or "").split("-")[0]
    if not series:
        return []
    end = int(now_utc().timestamp())
    start = end - days * 86400
    raw = http_json(f"{KALSHI}/trade-api/v2/series/{urllib.parse.quote(series)}"
                    f"/markets/{urllib.parse.quote(ticker)}/candlesticks"
                    f"?start_ts={start}&end_ts={end}&period_interval=1440")
    out = []
    for candle in raw.get("candlesticks") or []:
        price = candle.get("price") or {}
        value = as_float(price.get("close_dollars")) or as_float(price.get("mean_dollars"))
        stamp_ts = candle.get("end_period_ts")
        if value is None or not stamp_ts:
            continue
        out.append({"date": datetime.fromtimestamp(stamp_ts, timezone.utc).strftime("%Y-%m-%d"),
                    "p": round(value, 4)})
    return out


def detail_kalshi(ticker):
    raw = http_json(f"{KALSHI}/trade-api/v2/markets/{urllib.parse.quote(ticker)}")
    markets = parse_kalshi_v2_markets({"markets": [raw.get("market", {})]})
    if not markets:
        raise LookupError(f"no active Kalshi market {ticker}")
    market = markets[0]
    try:
        market["history"] = kalshi_history(ticker)
        day, week = changes_from_history(market["history"])
        if market["prob_24h_change"] is None:
            market["prob_24h_change"] = day
        if market["prob_7d_change"] is None:
            market["prob_7d_change"] = week
    except Exception as exc:
        market["history_error"] = str(exc)
    return stamp(market)


# --------------------------------------------------------------------------
# Manifold  (play money — always labelled as such)
# --------------------------------------------------------------------------

def parse_manifold_search(raw):
    out = []
    for m in raw or []:
        if m.get("isResolved"):
            continue
        prob = as_float(m.get("probability"))
        market = blank_market("manifold")
        end = m.get("closeTime")
        end_iso = (datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()
                   if isinstance(end, (int, float)) else None)
        market.update({
            "id": m.get("id"),
            "title": m.get("question"),
            "outcome": "Yes" if m.get("outcomeType") == "BINARY" else m.get("outcomeType"),
            "probability": prob,
            "participants": m.get("uniqueBettorCount"),
            "participants_label": "bettors",
            "end_date": end_iso,
            "end_date_passed": bool(end_iso and parse_iso(end_iso) < now_utc()),
            "url": m.get("url"),
            # Mana, not dollars — deliberately not written into volume_usd.
            "volume_mana": as_float(m.get("volume")),
        })
        market["display_title"] = _display_title(market)
        label_outcomes(market, ["Yes", "No"],
                       [prob, (1 - prob) if prob is not None else None])
        out.append(finalize(market))
    return out


def search_manifold(keyword, limit=6):
    url = f"{MANIFOLD}/search-markets?term={urllib.parse.quote(keyword)}&limit={limit}"
    return parse_manifold_search(http_json(url))


def detail_manifold(market_id):
    raw = http_json(f"{MANIFOLD}/market/{urllib.parse.quote(market_id)}")
    markets = parse_manifold_search([raw])
    if not markets:
        raise LookupError(f"no open Manifold market {market_id}")
    return stamp(markets[0])


# --------------------------------------------------------------------------
# Metaculus  (best effort: API went login-only in 2026, page is behind Cloudflare)
# --------------------------------------------------------------------------

def _scrapling_binary():
    """Optional dependency, looked up in this order so no path is baked in:
    PREDICTION_MARKET_SCRAPLING, then PATH, then the venv layout the
    scrapling docs suggest."""
    explicit = os.environ.get("PREDICTION_MARKET_SCRAPLING")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("scrapling")
    if found:
        return found
    fallback = os.path.expanduser("~/.scrapling-venv/bin/scrapling")
    return fallback if os.path.exists(fallback) else None


def metaculus_available():
    return _scrapling_binary() is not None


def search_metaculus(keyword, limit=4):
    if not metaculus_available():
        raise RuntimeError(
            "metaculus needs the scrapling CLI on PATH or PREDICTION_MARKET_SCRAPLING")
    binary = _scrapling_binary()
    url = f"https://www.metaculus.com/questions/?search={urllib.parse.quote(keyword)}"
    out_path = f"/tmp/pm_metaculus_{abs(hash(keyword)) % 10**8}.md"
    try:
        subprocess.run([binary, "extract", "stealthy-fetch", url, out_path, "--network-idle"],
                       capture_output=True, timeout=90, check=False)
        with open(out_path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception as exc:
        raise RuntimeError(f"metaculus scrape failed: {exc}")
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)

    out = []
    pattern = re.compile(r"\[([^\]]{12,140}\?)\]\((https://www\.metaculus\.com/questions/[^)]+)\)")
    seen = set()
    for title, link in pattern.findall(text):
        if link in seen:
            continue
        seen.add(link)
        tail = text.split(link, 1)[1][:200] if link in text else ""
        pct = re.search(r"(\d{1,3})%", tail)
        market = blank_market("metaculus")
        market.update({
            "id": link.rstrip("/").split("/")[-1],
            "title": title.strip(),
            "outcome": "community forecast",
            "probability": int(pct.group(1)) / 100 if pct else None,
            "participants_label": "forecasters",
            "url": link,
        })
        out.append(finalize(market))
        if len(out) >= limit:
            break
    return out


TICKER_RE = re.compile(r"\(([A-Z]{1,6})\)")
WINDOW_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "week", "weekly", "month",
    "monthly", "daily", "quarter", "eod", "tomorrow",
}
TOUCH_WORDS = ("at any point", "any time", "anytime", "touch", "hit", "dip", "reach")
CLOSE_WORDS = ("closing price", "close above", "closes above", "finish", "final price")


def _is_touch_market(market):
    """Only "reached at any point in the window" markets nest by window
    length. Closing above a level on the 21st and on the 31st are unrelated
    questions, so comparing them would invent a violation."""
    rules = str(market.get("rules") or "").lower()
    if any(word in rules for word in CLOSE_WORDS):
        return False
    haystack = f"{market.get('title') or ''} {market.get('outcome') or ''} {rules}".lower()
    return any(word in haystack for word in TOUCH_WORDS)


def _underlying(market):
    """What the market is about, ignoring the window it covers."""
    title = str(market.get("event_title") or market.get("title") or "")
    ticker = TICKER_RE.search(title)
    if ticker:
        return ("ticker", ticker.group(1))
    tokens = content_tokens(title) - {stem(w) for w in WINDOW_WORDS}
    return ("tokens", frozenset(tokens))


def _same_underlying(a, b, extra_allowed=1):
    """Same subject, not merely a shared word.

    "Will MicroStrategy announce holding ___ BTC" shares `bitcoin` with a
    Bitcoin price ladder, and treating one shared word as identity produced a
    cross-market warning between a company's holdings and a coin's price.
    The smaller description has to be contained in the larger, with room for
    a qualifier or two — not for a whole other subject.
    """
    kind_a, val_a = a
    kind_b, val_b = b
    if kind_a != kind_b:
        return False
    if kind_a == "ticker":
        return val_a == val_b
    if not val_a or not val_b:
        return False
    small, large = (val_a, val_b) if len(val_a) <= len(val_b) else (val_b, val_a)
    return small <= large and len(large - small) <= extra_allowed


def check_windows(markets):
    """Flag the same level priced higher over a shorter window.

    A level touched during the week of 17 August is necessarily touched
    during August, so the weekly rung can never price above the monthly one.
    Polymarket had them at 95.5% and 87.5%. The per-event ladder check cannot
    see this: the rungs live in different events.
    """
    groups = {}
    for market in markets:
        if market.get("probability") is None or not _is_touch_market(market):
            continue
        parts = _ladder_parts(market)
        end = parse_iso(market.get("end_date"))
        if not parts or not end:
            continue
        shape, value = parts
        groups.setdefault((market.get("source"), shape, value), []).append(
            (end, market, _underlying(market)))

    for entries in groups.values():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda item: item[0])
        for i, (short_end, short_m, short_u) in enumerate(entries):
            for long_end, long_m, long_u in entries[i + 1:]:
                if short_end >= long_end or not _same_underlying(short_u, long_u):
                    continue
                gap = short_m["probability"] - long_m["probability"]
                if gap <= LADDER_TOLERANCE:
                    continue
                note = (f"⚠️ Window inconsistent: {short_m['probability']:.1%} by "
                        f"{short_end.date()} but only {long_m['probability']:.1%} by "
                        f"{long_end.date()} for the same level — the shorter window "
                        f"cannot be likelier")
                for market in (short_m, long_m):
                    if note not in market["flags"]:
                        market["flags"].append(note)


CROSS_EVENT_DAYS = 7
# Comparing two thresholds needs the deadlines to be the same deadline, not
# merely the same week: `.days` truncation let 7 days 23 hours pass as 7, so
# a market ending 24 August was checked against one ending 1 September.
SAME_DEADLINE_HOURS = 36


def _same_deadline(a, b):
    return abs((a - b).total_seconds()) <= SAME_DEADLINE_HOURS * 3600


def check_cross_event_thresholds(markets):
    """Flag the same underlying priced out of order across separate events.

    One venue had "hit $170k in 2026" at 2.15% and a standalone "hit $150k by
    31 Dec 2026" at 1.25%. Touching 170k means touching 150k on the way, so
    that ordering cannot hold. Both the per-event ladder check and the
    nested-window check miss it: same window, different thresholds, different
    events.
    """
    entries = []
    for market in markets:
        if market.get("probability") is None or not _is_touch_market(market):
            continue
        # Read the level the way `_level_of` does: a rung label that carries
        # no direction is a date or a name, not a threshold, and comparing
        # "by December 31, 2026" as the number 31 against a $90,000 rung
        # manufactured eight contradictions at once.
        value = _level_of(market)
        end = parse_iso(market.get("end_date"))
        if value is None or not end:
            continue
        parts = _ladder_parts(market)
        direction = _expected_direction(parts[0] if parts else None,
                                        market.get("event_title") or market.get("title"))
        if direction is None:
            continue
        entries.append((market, value, direction, end, _underlying(market)))

    for i, (m_a, v_a, dir_a, end_a, u_a) in enumerate(entries):
        for m_b, v_b, dir_b, end_b, u_b in entries[i + 1:]:
            if m_a is m_b or v_a == v_b or dir_a != dir_b:
                continue
            if not _same_deadline(end_a, end_b):
                continue
            if (m_a.get("event_title") or m_a.get("title")) == (m_b.get("event_title") or m_b.get("title")):
                continue  # same event — the ladder check owns this
            if not _same_underlying(u_a, u_b):
                continue
            low, high = (m_a, m_b) if v_a < v_b else (m_b, m_a)
            delta = high["probability"] - low["probability"]
            if delta * dir_a < -LADDER_TOLERANCE:
                note = (f"⚠️ Cross-market inconsistency: {high['outcome']} prints "
                        f"{high['probability']:.1%} but {low['outcome']} only "
                        f"{low['probability']:.1%}, same underlying and deadline")
                for market in (low, high):
                    if note not in market["flags"]:
                        market["flags"].append(note)


# The suffix must not be the first letter of the next word: "$149999.99 by
# Dec 31" was read as 1.5e14 because the b of "by" counted as billions.
TITLE_LEVEL = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)\s*([kmb])?(?![a-z0-9])"
    r"|(\d[\d,]*(?:\.\d+)?)\s*([kmb])(?![a-z0-9])",
    re.I)
SAME_LEVEL_TOLERANCE = 0.02
SAME_LEVEL_RATIO = 1.5
SAME_LEVEL_FLOOR = 0.01


def _level_of(market):
    """The price level a market is about, from its rung label or, failing
    that, from its title.

    A standalone "Will Bitcoin hit $150k by December 31, 2026?" keeps its
    level in the title and its rung label is a date, so every check that
    read only labels skipped it — including the one that should have caught
    it printing half of what the ladder's own 150,000 rung printed.
    """
    parts = _ladder_parts(market)
    if parts and _expected_direction(parts[0], None) is not None:
        return parts[1]
    found = TITLE_LEVEL.search(str(market.get("event_title") or market.get("title") or ""))
    if not found:
        return None
    digits = found.group(1) or found.group(3)
    suffix = (found.group(2) or found.group(4) or "").lower()
    value = as_float((digits or "").replace(",", ""))
    return value * SUFFIX_SCALE.get(suffix, 1) if value is not None else None


def check_same_level(markets):
    """Flag one level priced two different ways on the same venue.

    Polymarket's ladder rung ↑150,000 printed 2.5% while its own standalone
    "hit $150k by December 31, 2026" printed 1.25% — same level, same
    deadline, both over a million dollars traded, twice apart.
    """
    entries = []
    for market in markets:
        level = _level_of(market)
        end = parse_iso(market.get("end_date"))
        if level is None or end is None or market.get("probability") is None:
            continue
        if not _is_touch_market(market):
            continue
        entries.append((market, level, end, _underlying(market)))

    for i, (m_a, lvl_a, end_a, u_a) in enumerate(entries):
        for m_b, lvl_b, end_b, u_b in entries[i + 1:]:
            if m_a.get("event_id") == m_b.get("event_id"):
                continue
            # One venue quoting itself two ways is a contradiction. A
            # real-money market differing from play money is not, and using
            # a 33-bettor market to accuse one of being wrong inverts the
            # skill's own ranking of the venues.
            if m_a.get("source") != m_b.get("source") or m_a.get("source") not in REAL_MONEY:
                continue
            if lvl_a != lvl_b or not _same_deadline(end_a, end_b):
                continue
            if not _same_underlying(u_a, u_b):
                continue
            hi = max(m_a["probability"], m_b["probability"])
            lo = min(m_a["probability"], m_b["probability"])
            # On small probabilities the absolute gap stays tiny while the
            # disagreement is total: 2.5% against 1.25% is 1.25 points and
            # twice the price.
            wide = (hi - lo) > SAME_LEVEL_TOLERANCE
            doubled = hi >= SAME_LEVEL_FLOOR and lo > 0 and hi / lo >= SAME_LEVEL_RATIO
            if not (wide or doubled):
                continue
            # Written per row: one shared sentence meant "here" pointed at
            # the other market's number on whichever row it was appended to.
            for market, other in ((m_a, m_b), (m_b, m_a)):
                note = (f"⚠️ Same level priced twice: {market['probability']:.1%} here vs "
                        f"{other['probability']:.1%} on {other.get('display_title') or 'another market'}"
                        f" — same level and deadline, so at least one is wrong")
                if note not in market["flags"]:
                    market["flags"].append(note)


# --------------------------------------------------------------------------
# Underlying spot price
# --------------------------------------------------------------------------
# "52% chance it touches $520" says nothing until you know where the price is
# now. Ladder questions are unanswerable without an anchor, and inventing one
# is exactly what this skill is built not to do.

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"

# Yahoo resolves bare crypto tickers to equities: "BTC" is Grayscale's
# Bitcoin Mini Trust at $28, not Bitcoin at $64,000. Anchoring a $150k ladder
# to a $28 share price is the exact misreading the anchor step exists to
# prevent, so the coin form is chosen explicitly.
CRYPTO_TICKERS = {
    "BTC", "XBT", "ETH", "SOL", "DOGE", "XRP", "ADA", "AVAX", "LINK", "DOT",
    "MATIC", "BNB", "LTC", "TRX", "SHIB", "TON", "ATOM", "NEAR", "APT", "ARB",
}
CRYPTO_NAMES = {
    "BITCOIN": "BTC-USD", "ETHEREUM": "ETH-USD", "ETHER": "ETH-USD",
    "SOLANA": "SOL-USD", "DOGECOIN": "DOGE-USD", "RIPPLE": "XRP-USD",
}


def resolve_symbol(symbol):
    """Map a bare crypto ticker or coin name onto Yahoo's pair form."""
    raw = (symbol or "").strip().upper()
    if not raw or "-" in raw:
        return raw
    if raw in CRYPTO_NAMES:
        return CRYPTO_NAMES[raw]
    if raw in CRYPTO_TICKERS:
        return f"{raw}-USD"
    return raw


def parse_spot(raw):
    meta = ((raw.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    price = as_float(meta.get("regularMarketPrice"))
    previous = as_float(meta.get("chartPreviousClose")) or as_float(meta.get("previousClose"))
    return {
        "symbol": meta.get("symbol"),
        # Naming the instrument is what makes a wrong ticker obvious:
        # "Grayscale Bitcoin Mini Trust ETF" beside a $150k ladder is
        # visibly not Bitcoin.
        "name": meta.get("longName") or meta.get("shortName"),
        "instrument_type": meta.get("instrumentType"),
        "price": price,
        "previous_close": previous,
        "change_pct": round((price / previous - 1) * 100, 2) if price and previous else None,
        "fifty_two_week_low": as_float(meta.get("fiftyTwoWeekLow")),
        "fifty_two_week_high": as_float(meta.get("fiftyTwoWeekHigh")),
        "currency": meta.get("currency"),
        "source": "Yahoo Finance — underlying spot, not a market price",
    }


def fetch_spot(symbol):
    resolved = resolve_symbol(symbol)
    raw = http_json(f"{YAHOO}/{urllib.parse.quote(resolved)}?interval=1d&range=5d")
    quote = parse_spot(raw)
    if quote["price"] is None:
        raise LookupError(f"no quote for {symbol}")
    if resolved != (symbol or "").strip().upper():
        quote["requested"] = symbol
        quote["note"] = f"{symbol} resolves to an equity on Yahoo; used {resolved} instead"
    return stamp(quote)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

SEARCHERS = {
    "polymarket": search_polymarket,
    "kalshi": search_kalshi,
    "manifold": search_manifold,
    "metaculus": search_metaculus,
}

DETAILERS = {
    "polymarket": detail_polymarket,
    "kalshi": detail_kalshi,
    "manifold": detail_manifold,
}


def _activity(market):
    """Lifetime volume first. Ranking on 24h flow let one quiet day sink an
    $8.4M market below a $1.25M one."""
    return (market.get("volume_usd") or market.get("volume_7d_usd")
            or market.get("volume_24h_usd") or market.get("event_volume_24h_usd") or 0)


# --------------------------------------------------------------------------
# Relevance
# --------------------------------------------------------------------------
# Every venue's search is fuzzy and never returns nothing. Asking "will I get
# promoted next year" pulls back the Los Angeles mayoral race; "will my cat
# learn to play piano" pulls back Super Bowl halftime performers. Passing
# those upward as candidates is how a query-only skill ends up bluffing, so
# the gate lives here in code rather than in an instruction the model may
# reason its way around.

STOPWORDS = {
    "will", "would", "can", "could", "should", "shall", "may", "might", "must",
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "there", "here", "is", "are", "was", "were", "be", "been",
    "being", "do", "does", "did", "done", "have", "has", "had", "get", "gets",
    "got", "go", "goes", "going", "to", "of", "in", "on", "at", "by", "for",
    "with", "from", "as", "into", "about", "before", "after", "during", "next",
    "last", "my", "me", "i", "we", "our", "you", "your", "it", "its", "he",
    "she", "they", "them", "his", "her", "their", "any", "all", "some", "no",
    "not", "up", "down", "out", "over", "under", "again", "happen", "happens",
    "occur", "become", "becomes", "chance", "chances", "odds", "probability",
    "likely", "possible", "market", "markets", "prediction", "predict",
    # Temporal filler: shared by half of all markets, so it proves nothing.
    "year", "years", "today", "tomorrow", "yesterday", "week", "weeks",
    "month", "months", "day", "days", "time", "times", "date", "end", "start",
    "soon", "ever", "now",
    # Finance filler. These appear in a large share of market titles, so
    # letting them count as matches lets a Bitcoin ladder answer a question
    # about Meta — which is exactly what happened.
    "price", "prices", "priced", "stock", "stocks", "share", "shares",
    "above", "below", "over", "under", "close", "closes", "closing", "hit",
    "hits", "reach", "reaches", "high", "higher", "low", "lower", "cap",
    "level", "levels", "value", "target", "trade", "trades", "trading",
    "move", "moves", "rise", "rises", "fall", "falls", "drop", "drops",
    # Country filler: too common in titles to discriminate.
    "us", "usa", "u", "s",
    # Interrogatives. Every venue titles markets "Which party will…",
    # "What will X hit…", so these match everything and identify nothing.
    "what", "which", "who", "whom", "whose", "when", "where", "how", "why",
    "whether",
}

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Venues name the same thing two ways in the same breath: a search for
# "Bitcoin" threw away every market titled "BTC ...". Each group collapses to
# one canonical stem on both sides of the match.
ALIASES = {
    "btc": "bitcoin", "xbt": "bitcoin",
    "eth": "ethereum", "ether": "ethereum",
    "sol": "solana", "doge": "dogecoin", "xrp": "ripple",
    "tsla": "tesla", "aapl": "apple", "msft": "microsoft", "nvda": "nvidia",
    "googl": "alphabet", "goog": "alphabet", "google": "alphabet",
    "amzn": "amazon", "meta": "meta", "fb": "meta",
    "spx": "sp500", "sp": "sp500", "gop": "republican", "dem": "democrat",
    "dems": "democrat", "fomc": "fed",
}

# Crude but symmetric: applied to both sides, so "impeachment" and
# "impeached" meet at the same string even though neither is a real root.
SUFFIXES = ("ments", "ment", "tions", "tion", "sions", "sion", "ances",
            "ance", "ences", "ence", "ings", "ing", "ed")
MIN_STEM = 3


def stem(token):
    for suffix in SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM:
            return token[: -len(suffix)]
    # Plural -s, but never off a word that simply ends in a hiss: "recess"
    # must not collapse onto "recession".
    if (token.endswith("s") and not token.endswith(("ss", "us", "is"))
            and len(token) - 1 >= MIN_STEM):
        return token[:-1]
    return token


def _content_pairs(text):
    for token in TOKEN_RE.findall((text or "").lower()):
        # Amounts, years and tickers-with-digits ("150k", "2026", "25bps")
        # discriminate nothing, and letting one count as a match is how an
        # unrelated market clears a two-word bar.
        if token in STOPWORDS or token[0].isdigit() or len(token) < 2:
            continue
        stemmed = stem(token)
        yield ALIASES.get(stemmed, stemmed), len(token)


def content_tokens(text):
    """Words that actually carry the question. Bare numbers are excluded:
    a shared year is not evidence two questions are about the same thing."""
    return {s for s, _ in _content_pairs(text)}


def token_weights(text):
    """Stem -> length of the longest word it came from.

    Distinctiveness is judged on the word the user actually wrote:
    "recession" stems to a five-letter fragment, and scoring the fragment
    would demote the one word that identifies the question.
    """
    weights = {}
    for stemmed, length in _content_pairs(text):
        weights[stemmed] = max(weights.get(stemmed, 0), length)
    return weights


def filter_relevant(markets, keywords):
    """Keep markets that clear the bar for at least one keyword group.

    Groups are scored **independently**. Unioning them and then demanding two
    matches from the union made every extra phrasing tighten the filter — one
    group found Polymarket's Meta market, three groups lost it — which is the
    opposite of what "try a few phrasings" is supposed to do.

    Fails open: a query with no content words has nothing to match on, and
    returning nothing would masquerade as "no market exists".
    """
    groups = [w for w in (token_weights(k) for k in keywords) if w]
    if not groups:
        return markets

    kept = []
    for market in markets:
        haystack = content_tokens(
            f"{market.get('title') or ''} {market.get('outcome') or ''}")
        best = None
        for weights in groups:
            matched = sorted(set(weights) & haystack)
            if not matched:
                continue
            # One shared word is usually coincidence — "US recession" against
            # "2Y US Treasury yield today?" shares `us` and nothing that
            # matters. Ask for two, unless the single word carries the
            # question by itself, or the group only has one word to give.
            # Two matches, unless the group has only one word to give. A
            # single match out of several leaves the part of the question
            # that makes it specific unaccounted for — that is how a
            # Stanford football game answered a college-admissions question.
            needed = min(2, len(weights))
            if len(matched) >= needed:
                if best is None or len(matched) > len(best):
                    best = matched
        if best is not None:
            market["matched"] = best
            kept.append(market)
    return kept


# --------------------------------------------------------------------------
# Ladder integrity
# --------------------------------------------------------------------------
# A threshold ladder has to be monotone: whatever is true above $460 is also
# true above $440. Polymarket printed 92.4% for one and 90.0% for the other,
# on rungs with no volume behind them. Those are maker stubs, and they look
# exactly like prices.

LADDER_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[kKmMbB]?")
LADDER_TOLERANCE = 0.005
LADDER_MIN_RUNGS = 3


def _ladder_parts(market):
    """(shape, threshold) for a rung, where shape is the label with its number
    blanked out. Keeps '↓ $520' and '↑ $620' in separate ladders — each side
    is monotone on its own even though the combined sequence is not."""
    label = str(market.get("outcome") or "")
    found = LADDER_NUMBER.search(label)
    if not found:
        return None
    text = found.group(0).replace(",", "").strip()
    scale = SUFFIX_SCALE.get(text[-1].lower(), 1) if text and text[-1].isalpha() else 1
    value = as_float(text[:-1] if scale != 1 else text)
    if value is None:
        return None
    value *= scale
    # Blank every number, not just the first, so "25,000-29,999.99" and
    # "30,000-34,999.99" land in one group instead of two groups of one.
    shape = LADDER_NUMBER.sub("#", label).strip()
    return shape, value


# Which way a ladder must run. "Above $X" gets less likely as X rises;
# "↓ $X" — touching a level on the way down — gets more likely as X rises
# toward the current price. Reading the direction from the labels is what
# lets a wholly inverted ladder be recognised as inverted.
FALLING_MARKS = ("↑", "above", "over", "≥", ">", "at least", "or more", "or above", "+")
RISING_MARKS = ("↓", "below", "under", "≤", "<", "or less", "dip")
TITLE_FALLING = FALLING_MARKS + ("hit", "reach", "exceed")


def _expected_direction(shape, title):
    """-1 if probability should fall as the threshold rises, +1 if it should
    rise, None when the rungs are not thresholds at all.

    "Democrats hold exactly 46 seats" is a distribution and is supposed to
    peak in the middle; checking it for monotonicity guarantees a false
    warning, and the skill requires every warning to be relayed.
    """
    label = (shape or "").lower()
    if any(mark in label for mark in FALLING_MARKS):
        return -1
    if any(mark in label for mark in RISING_MARKS):
        return 1
    # Only when the rung itself is silent does the event title get a say —
    # otherwise "hit" in "What will META hit in August?" would override the
    # ↓ on its own downside rungs.
    heading = (title or "").lower()
    if any(mark in heading for mark in TITLE_FALLING):
        return -1
    if any(mark in heading for mark in RISING_MARKS):
        return 1
    return None


def check_ladders(markets):
    """Flag rungs whose prices contradict each other. Mutates `markets`."""
    groups = {}
    for market in markets:
        parts = _ladder_parts(market)
        if not parts or market.get("probability") is None:
            continue
        shape, value = parts
        title = market.get("event_title") or market.get("title")
        groups.setdefault((market.get("source"), title, shape), []).append((value, market))

    for (_, title, shape), rungs in groups.items():
        direction = _expected_direction(shape, title)
        if direction is None:
            continue
        if len(rungs) < LADDER_MIN_RUNGS:
            continue
        rungs.sort(key=lambda pair: pair[0])
        # Every step running against the ladder's own direction is broken.
        # Reporting only the largest one hid the inversion that happened to
        # sit on the question being asked.
        offenders = []
        for (_, lo_m), (_, hi_m) in zip(rungs, rungs[1:]):
            delta = hi_m["probability"] - lo_m["probability"]
            if delta * direction < -LADDER_TOLERANCE:
                offenders.append((lo_m, hi_m))
        if not offenders:
            continue
        detail = "; ".join(
            f"{hi_m['outcome']} {hi_m['probability']:.1%} vs {lo_m['outcome']} "
            f"{lo_m['probability']:.1%}" for lo_m, hi_m in offenders[:8])
        note = (f"⚠️ Ladder inconsistent ({len(offenders)}): {detail} "
                f"— some rungs are unquoted stubs")
        for _, market in rungs:
            if note not in market["flags"]:
                market["flags"].append(note)


STUB_PRICE = 0.5


def drop_stubs(markets):
    """Remove untouched placeholder contracts.

    Polymarket seeds a field with unnamed rows — "Team A", "Team B", "Other"
    — sitting at exactly 0.5 with no volume. Once a named field is ordered by
    probability, those stubs print above the real 23% favourite. A market
    nobody has traded, still at the seeded coin flip, holds no view at all.
    """
    kept = []
    for market in markets:
        traded = (market.get("volume_usd") or market.get("volume_24h_usd")
                  or market.get("volume_7d_usd") or market.get("volume_mana"))
        if not traded and market.get("probability") == STUB_PRICE:
            continue
        kept.append(market)
    return kept


def drop_unpriced(markets):
    """No number, no answer. Multi-outcome Manifold markets and Metaculus
    pages we failed to read land here."""
    return [m for m in markets if m.get("probability") is not None]


def build_search_payload(keywords, sources, errors, markets):
    """Deliberately carries no cross-venue divergence claim.

    At search time nothing establishes that a Polymarket row and a Kalshi row
    describe the same outcome, and comparing mismatched rows manufactures
    nonsense like "70 point spread" out of 'Cut 25bps' versus 'No change'.
    Match them first, then run the compare command.
    """
    payload = {
        "fetched_at": now_utc().isoformat(timespec="seconds"),
        "keywords": keywords,
        "sources_queried": list(sources),
        "candidates": markets,
    }
    if errors:
        payload["unavailable"] = errors
    if markets:
        payload["verdict"] = "found"
    elif errors and all(source in errors for source in sources):
        # Every venue asked failed or was not installed. Reporting
        # "no market" here would turn a broken run into a confident answer.
        payload["verdict"] = "sources_unavailable"
        payload["note"] = ("No venue could be queried, so nothing is known. "
                           "This is not evidence that no market exists.")
    else:
        payload["verdict"] = "no_live_market"
    if not markets and payload["verdict"] == "no_live_market":
        payload["note"] = ("No live market covers this question. Say so plainly; "
                           "do not substitute an estimate of your own.")
    return payload


NUMBER_WITH_SUFFIX = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kmb])?", re.I)
SUFFIX_SCALE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def keyword_numbers(keywords):
    """Amounts the asker named, so their rung survives the per-event cap.

    A 32-rung Bitcoin ladder was capped at 20 by volume and the rung it
    dropped was 150,000 — in a search whose keyword group was "Bitcoin 150k".
    """
    found = set()
    for keyword in keywords:
        for digits, suffix in NUMBER_WITH_SUFFIX.findall(keyword or ""):
            value = as_float(digits.replace(",", ""))
            if value is not None:
                found.add(value * SUFFIX_SCALE.get((suffix or "").lower(), 1))
    return found


DISTRIBUTION_MIN = 0.97
# A complete futures board runs a few points over 100% on the spread alone,
# so the ceiling has to sit above ordinary margin.
DISTRIBUTION_MAX = 1.12
# Past this, whatever the field is, it is not one-winner.
DISTRIBUTION_CEILING = 1.5

# Fields where several outcomes win at once: sixteen teams make the playoffs,
# so thirty "will they make it" markets are supposed to sum near 1600%.
# "seats" is deliberately absent: "How many seats will they hold?" is a
# proper distribution over mutually exclusive counts, not a multi-winner field.
MULTI_WINNER_MARKS = ("playoff", "qualify", "advance", "make the", "reach the",
                      "nominat", "shortlist", "top 4", "top four", "medal",
                      "relegat", "promot")


CUMULATIVE_TITLE_MARKS = ("when will", "how soon", "when does", "when is")
CUMULATIVE_LABEL_MARKS = ("before ", "by ", "on or before")


def _is_cumulative_field(title, rungs):
    """Nested deadlines rather than alternatives.

    "When will Bitcoin cross $100k again?" lists before-September,
    before-October, before-January — each window contains the last, so the
    prices are supposed to climb and their sum means nothing. Treating them
    as a distribution reported "78% sits on outcomes not shown" about a field
    that was complete.
    """
    if any(mark in (title or "").lower() for mark in CUMULATIVE_TITLE_MARKS):
        return True
    labelled = sum(1 for m in rungs
                   if str(m.get("outcome") or "").lower().startswith(CUMULATIVE_LABEL_MARKS))
    return labelled * 2 >= len(rungs)


def check_distribution(markets):
    """Mutually exclusive fields must add up. Mutates `markets`.

    Twenty Polymarket Nobel candidates summed to 36% — two thirds of the
    probability sat on names never returned — and eighteen Kalshi candidates
    summed to 119%, which no coherent set of prices can do. Neither was
    mentioned, because the old check lived behind a regex that required a
    digit in the label and so never saw a field of people's names.

    Touch ladders are exempt: touching 540 and touching 520 are not
    alternatives, so they are supposed to sum past 100%.
    """
    groups = {}
    for market in markets:
        if market.get("probability") is None:
            continue
        groups.setdefault(_event_key(market), []).append(market)

    for rungs in groups.values():
        if len(rungs) < 4:
            continue
        parts = [_ladder_parts(m) for m in rungs]
        title = (rungs[0].get("event_title") or rungs[0].get("title") or "")
        if any(p and _expected_direction(p[0], title) for p in parts):
            continue  # thresholds, not alternatives
        if any(mark in title.lower() for mark in MULTI_WINNER_MARKS):
            continue  # several outcomes win at once; the sum means nothing
        if _is_cumulative_field(title, rungs):
            continue  # nested deadlines, each containing the last
        total = sum(m["probability"] for m in rungs)
        if total >= DISTRIBUTION_CEILING:
            continue  # not a one-winner board, whatever it is
        note = None
        if total < DISTRIBUTION_MIN:
            note = (f"⚠️ Distribution incomplete: the {len(rungs)} outcomes returned sum "
                    f"to {total:.0%}, so {1 - total:.0%} of the probability sits on "
                    f"outcomes the venue did not return — do not read these as the "
                    f"whole field, and do not expect another page to reveal them")
        elif total > DISTRIBUTION_MAX:
            note = (f"⚠️ Distribution incoherent: {len(rungs)} outcomes sum to {total:.0%} "
                    f"— either the quotes contradict each other or these outcomes are "
                    f"not actually mutually exclusive")
        if not note:
            continue
        for market in rungs:
            if note not in market["flags"]:
                market["flags"].append(note)


def order_rungs(markets):
    """Keep an event's rungs together and in threshold order.

    Ranking alone interleaves ↑ and ↓ rungs of one ladder by volume, so the
    caller has to reassemble the ladder by parsing arrows out of the labels
    before it can read the distribution — the shape that answers "how far".
    """
    order, grouped = [], {}
    for market in markets:
        key = _event_key(market)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(market)

    out = []
    for key in order:
        rungs = grouped[key]
        parts = [(_ladder_parts(m), m) for m in rungs]
        if all(p for p, _ in parts) and len(rungs) > 2:
            rungs = [m for _, m in sorted(parts, key=lambda item: (item[0][0], item[0][1]))]
        elif len(rungs) > 2:
            # A field of named candidates has no natural order, so volume
            # decided it — and printed the 8.5% favourite last, behind a
            # 2.45% long shot with a deeper book.
            rungs = sorted(rungs, key=lambda m: -(m.get("probability") or 0))
        out.extend(rungs)
    return out


SHORT_HORIZON_DAYS = 2
LONG_HORIZON_DAYS = 14


def _horizon_tier(market, pool):
    """Demote markets expiring within days, but only when longer-dated
    candidates exist. A next-day "BTC above X at 8pm" event outranked every
    rung of the year-end ladder; when the question really is about today,
    nothing here changes."""
    end = parse_iso(market.get("end_date"))
    if not end:
        return 0
    now = now_utc()
    if (end - now).days > SHORT_HORIZON_DAYS:
        return 0
    for other in pool:
        other_end = parse_iso(other.get("end_date"))
        if other_end and (other_end - now).days > LONG_HORIZON_DAYS:
            return 1
    return 0


def rank_key(market):
    """Relevance first, then money. A market that matches more of the
    question beats one that merely trades more."""
    return (-len(market.get("matched") or []), -_activity(market))


def _event_key(market):
    """Identity, not display text. Polymarket ships two distinct events both
    titled "OpenAI IPO Closing Market Cap"; grouping by title merged them and
    produced a completeness warning computed over half of each."""
    return (market.get("source"),
            market.get("event_id") or market.get("event_title") or market.get("title"))


MAX_RUNGS_PER_EVENT = 20


def run_search(keywords, sources, limit=8, show_dropped=False,
               max_outcomes=MAX_RUNGS_PER_EVENT):
    jobs, results, errors = [], [], {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for source in sources:
            for keyword in keywords:
                jobs.append((source, keyword, pool.submit(SEARCHERS[source], keyword)))
        for source, keyword, future in jobs:
            try:
                results.extend(future.result())
            except Exception as exc:
                errors.setdefault(source, f"{type(exc).__name__}: {exc}")

    priced = drop_stubs(drop_unpriced(results))
    relevant = filter_relevant(priced, keywords)
    relevant_ids = {(m["source"], m["id"]) for m in relevant}
    dropped = [m for m in priced if (m["source"], m["id"]) not in relevant_ids]

    seen, deduped = set(), []
    for market in sorted(relevant, key=rank_key):
        key = (market["source"], market["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(market)

    # Trim whole events, never individual rungs. A 14-rung ladder is the
    # ideal shape for a "how far" question, and slicing the top four markets
    # off the pile destroys it without saying so.
    grouped, order = {}, []
    for market in deduped:
        key = _event_key(market)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(market)

    wanted_numbers = keyword_numbers(keywords)
    per_source, trimmed, skipped = {}, [], []
    for key in order:
        source = key[0]
        if per_source.get(source, 0) >= limit:
            skipped.append(key)
            continue
        per_source[source] = per_source.get(source, 0) + 1
        rungs = grouped[key]
        if len(rungs) > max_outcomes and wanted_numbers:
            asked = [m for m in rungs
                     if (_ladder_parts(m) or (None, None))[1] in wanted_numbers]
            rungs = asked + [m for m in rungs if m not in asked]
        trimmed.extend(rungs[:max_outcomes])

    check_ladders(trimmed)
    check_distribution(trimmed)
    check_windows(trimmed)
    check_cross_event_thresholds(trimmed)
    check_same_level(trimmed)
    trimmed.sort(key=lambda m: (_horizon_tier(m, trimmed),) + rank_key(m))
    trimmed = order_rungs(trimmed)

    payload = build_search_payload(keywords, sources, errors, trimmed)
    payload["events"] = [
        {
            "source": key[0],
            "title": (grouped[key][0].get("event_title")
                      or grouped[key][0].get("title")),
            "outcomes_returned": len(grouped[key][:max_outcomes]),
            # What this search matched, NOT the venue's outcome count — the
            # same event can match 18 rungs on one query and 3 on another,
            # so this cannot prove a ladder came back whole.
            "outcomes_matched": len(grouped[key]),
            # Sum what was returned, so this and the distribution flag speak
            # about the same set of rows.
            "outcomes_sum": round(sum(m["probability"] for m in grouped[key][:max_outcomes]
                                      if m.get("probability") is not None), 4),
            "possibly_truncated": len(grouped[key]) > max_outcomes,
            "url": grouped[key][0].get("url"),
        }
        for key in order if any(m in trimmed for m in grouped[key])
    ]
    if skipped:
        # Silent truncation reads as "this is everything there is".
        payload["events_not_returned"] = [
            {"source": key[0],
             "title": (grouped[key][0].get("event_title") or grouped[key][0].get("title")),
             "outcomes": len(grouped[key])}
            for key in skipped]
        payload["limit_note"] = (f"--limit {limit} events per venue; {len(skipped)} more "
                                 f"matched and were not returned. Raise --limit to see them.")
    if dropped:
        payload["dropped_as_irrelevant"] = len(dropped)
        if show_dropped:
            payload["dropped_examples"] = [
                {"source": m["source"], "title": m.get("title"),
                 "outcome": m.get("outcome"), "matched": m.get("matched", [])}
                for m in dropped[:25]
            ]
    return payload


COMPARE_SLOP_PP = 5.0


RESOLUTION_CLASSES = {
    "completion": ("completes", "completed", "lists", "listed", "begins trading",
                   "closing price", "settles at"),
    "announcement": ("announces", "announced", "confirms", "confirmed", "files",
                     "filed", "declares"),
}


def _resolution_class(rules):
    """What the market pays on. A ladder resolving when a company *confirms*
    an IPO was compared against one resolving when it *completes* one, and
    the tool called a three-point gap agreement."""
    text = (rules or "").lower()
    for name, words in RESOLUTION_CLASSES.items():
        if any(word in text for word in words):
            return name
    return None


def compare_summary(markets):
    """The spread the output template asks for, computed rather than left to
    the reader — plus the caveats that make a spread meaningless."""
    probs = [m["probability"] for m in markets if m.get("probability") is not None]
    summary = {"spread_pp": None, "agree": None, "caveats": []}
    if len(probs) >= 2:
        spread = (max(probs) - min(probs)) * 100
        summary["spread_pp"] = round(spread, 1)
        hi, lo = max(probs), min(probs)
        # On small probabilities the point spread stays inside the slop while
        # one price is twice the other, which the search path already calls a
        # contradiction. Both paths now answer the same way.
        doubled = lo > 0 and hi / lo >= SAME_LEVEL_RATIO and hi >= SAME_LEVEL_FLOOR
        summary["agree"] = spread <= COMPARE_SLOP_PP and not doubled
        if doubled:
            summary["caveats"].append(
                f"{hi:.1%} is {hi / lo:.1f}x {lo:.1%} — a small point spread, but the "
                f"two prices disagree by a wide margin at this probability")
    ends = [parse_iso(m.get("end_date")) for m in markets]
    known_ends = [e for e in ends if e]
    unverified = False
    if len(known_ends) < len(markets):
        unverified = True
        summary["caveats"].append(
            "Could not compare deadlines — at least one market reported no end date")
    elif (max(known_ends) - min(known_ends)).days > CROSS_EVENT_DAYS:
        summary["caveats"].append(
            f"Deadlines differ by {(max(known_ends) - min(known_ends)).days} days "
            f"({min(known_ends).date()} vs {max(known_ends).date()}). Annual events are "
            f"often listed with different expiry conventions, so check the rules before "
            f"treating this as two different questions")

    missing = [m.get("source") for m in markets if not m.get("rules")]
    if missing:
        unverified = True
        summary["caveats"].append(
            f"No resolution text from {', '.join(sorted(set(missing)))} — "
            f"cannot confirm both sides resolve on the same event")

    levels = {_level_of(m) for m in markets}
    levels.discard(None)
    if len(levels) > 1:
        unverified = True
        summary["caveats"].append(
            f"Different levels: {sorted(levels)} — these are not the same question")

    verbs = {_resolution_class(m.get("rules")) for m in markets}
    verbs.discard(None)
    if len(verbs) > 1:
        unverified = True
        summary["caveats"].append(
            f"Resolution differs: one side resolves on {' and the other on '.join(sorted(verbs))}"
            f" — confirming an event is not the same as completing it")

    if unverified:
        # "agree" claimed on markets resolving on different events is worse
        # than no comparison at all.
        summary["agree"] = None
        summary["caveats"].append("Spread reported, but agreement could not be verified")
    return summary


def run_compare(refs):
    """refs are 'source:id' pairs the caller has judged to be the same question."""
    markets, errors = [], {}
    for ref in refs:
        source, _, ident = ref.partition(":")
        if source not in DETAILERS:
            errors[ref] = f"unknown source {source!r}"
            continue
        try:
            markets.append(DETAILERS[source](ident))
        except Exception as exc:
            errors[ref] = f"{type(exc).__name__}: {exc}"
    payload = {"fetched_at": now_utc().isoformat(timespec="seconds"), "markets": markets}
    check_ladders(markets)
    check_windows(markets)
    check_cross_event_thresholds(markets)
    payload.update(compare_summary(markets))
    note = divergence_note(markets)
    if note:
        payload["divergence"] = note
    if errors:
        payload["unavailable"] = errors
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="search every venue for live markets")
    p_search.add_argument("keywords", nargs="+", help="English keyword groups")
    p_search.add_argument("--sources", default="polymarket,kalshi,manifold")
    p_search.add_argument("--limit", type=int, default=8, help="events kept per venue")
    p_search.add_argument("--max-outcomes", type=int, default=MAX_RUNGS_PER_EVENT,
                          help="outcomes kept per event (--limit trims events, not outcomes)")
    p_search.add_argument("--show-dropped", action="store_true",
                          help="list what the relevance gate rejected")

    p_detail = sub.add_parser("detail", help="deep data for one market")
    p_detail.add_argument("source", choices=sorted(DETAILERS))
    p_detail.add_argument("id")

    p_compare = sub.add_parser(
        "compare", help="compare markets you have judged to be the same question")
    p_compare.add_argument("refs", nargs="+", metavar="source:id")

    p_spot = sub.add_parser(
        "spot", help="underlying price for a ticker, to anchor a price ladder")
    p_spot.add_argument("symbol")

    args = parser.parse_args(argv)

    if args.command == "search":
        sources = [s.strip() for s in args.sources.split(",") if s.strip() in SEARCHERS]
        payload = run_search(args.keywords, sources, args.limit, args.show_dropped,
                             args.max_outcomes)
    elif args.command == "compare":
        payload = run_compare(args.refs)
    elif args.command == "spot":
        try:
            payload = fetch_spot(args.symbol)
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}", "symbol": args.symbol}
    else:
        try:
            payload = DETAILERS[args.source](args.id)
        except Exception as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}",
                       "source": args.source, "id": args.id}

    json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
