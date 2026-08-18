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
from datetime import datetime, timezone

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

    prob = market.get("probability")
    if prob is not None and (prob < NEAR_CERTAIN or prob > 1 - NEAR_CERTAIN):
        flags.append("Market treats this as all but settled")

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
            "clob_token_id": (json.loads(raw["clobTokenIds"])[0]
                              if isinstance(raw.get("clobTokenIds"), str) and raw["clobTokenIds"]
                              else None),
        })
        label_outcomes(market, _poly_labels(raw) or ["Yes", "No"], prices)
        out.append(finalize(market))
    return out


def changes_from_history(history):
    """(1-day, 1-week) deltas from the daily price series.

    Gamma's detail response only ships oneMonthPriceChange, so without this
    the deepest call would be the one that shows no trend.
    """
    if len(history) < 2:
        return (None, None)
    latest = history[-1]["p"]
    day = round(latest - history[-2]["p"], 4)
    week = round(latest - history[-8]["p"], 4) if len(history) >= 8 else None
    return (day, week)


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
            "end_date_passed": bool(parsed_end and parsed_end < now_utc()),
            "url": _kalshi_url(m.get("ticker")),
            "rules": m.get("rules_primary"),
        })
        label_outcomes(market, ["Yes", "No"],
                       [prob, (1 - prob) if prob is not None else None])
        out.append(finalize(market))
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
                # v1 search reports contract counts; each contract settles at $1.
                "volume_usd": as_float(m.get("volume")) or as_float(entry.get("total_volume")),
                # Venue-level, covering every sibling market — never this
                # market's own 24h figure.
                "event_volume_24h_usd": as_float(entry.get("recent_volume")),
                "participants_label": "open interest (Kalshi publishes no trader count)",
                "end_date": end,
                "url": _kalshi_url(entry.get("series_ticker") or ticker),
            })
            label_outcomes(market, ["Yes", "No"],
                           [prob, (1 - prob) if prob is not None else None])
            out.append(finalize(market))
    return out


def search_kalshi(keyword, limit=6):
    """The v1 search endpoint is undocumented. If it disappears, the caller
    reports Kalshi as unavailable rather than failing the whole query."""
    url = f"{KALSHI}/v1/search/series?query={urllib.parse.quote(keyword)}"
    return parse_kalshi_search(http_json(url))[: limit * 3]


def detail_kalshi(ticker):
    raw = http_json(f"{KALSHI}/trade-api/v2/markets/{urllib.parse.quote(ticker)}")
    markets = parse_kalshi_v2_markets({"markets": [raw.get("market", {})]})
    if not markets:
        raise LookupError(f"no active Kalshi market {ticker}")
    return stamp(markets[0])


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
        return []
    binary = _scrapling_binary()
    url = f"https://www.metaculus.com/questions/?search={urllib.parse.quote(keyword)}"
    out_path = f"/tmp/pm_metaculus_{abs(hash(keyword)) % 10**8}.md"
    try:
        subprocess.run([binary, "extract", "stealthy-fetch", url, out_path, "--network-idle"],
                       capture_output=True, timeout=90, check=False)
        with open(out_path, encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        return []
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


# --------------------------------------------------------------------------
# Underlying spot price
# --------------------------------------------------------------------------
# "52% chance it touches $520" says nothing until you know where the price is
# now. Ladder questions are unanswerable without an anchor, and inventing one
# is exactly what this skill is built not to do.

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"


def parse_spot(raw):
    meta = ((raw.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    price = as_float(meta.get("regularMarketPrice"))
    previous = as_float(meta.get("chartPreviousClose")) or as_float(meta.get("previousClose"))
    return {
        "symbol": meta.get("symbol"),
        "price": price,
        "previous_close": previous,
        "change_pct": round((price / previous - 1) * 100, 2) if price and previous else None,
        "fifty_two_week_low": as_float(meta.get("fiftyTwoWeekLow")),
        "fifty_two_week_high": as_float(meta.get("fiftyTwoWeekHigh")),
        "currency": meta.get("currency"),
        "source": "Yahoo Finance — underlying spot, not a market price",
    }


def fetch_spot(symbol):
    raw = http_json(f"{YAHOO}/{urllib.parse.quote(symbol)}?interval=1d&range=5d")
    quote = parse_spot(raw)
    if quote["price"] is None:
        raise LookupError(f"no quote for {symbol}")
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
    return (market.get("volume_24h_usd") or market.get("volume_7d_usd")
            or market.get("volume_usd") or 0)


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
}

TOKEN_RE = re.compile(r"[a-z0-9]+")

# Crude but symmetric: applied to both sides, so "impeachment" and
# "impeached" meet at the same string even though neither is a real root.
SUFFIXES = ("ments", "ment", "tions", "tion", "sions", "sion", "ances",
            "ance", "ences", "ence", "ings", "ing", "ed")
MIN_STEM = 3
DISTINCTIVE_WORD = 6


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
        if token in STOPWORDS or token.isdigit() or len(token) < 2:
            continue
        yield stem(token), len(token)


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
            needed = min(2, len(weights))
            distinctive = any(weights[t] >= DISTINCTIVE_WORD for t in matched)
            if len(matched) >= needed or distinctive:
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

LADDER_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
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
    value = as_float(found.group(0).replace(",", ""))
    if value is None:
        return None
    shape = (label[: found.start()] + "#" + label[found.end():]).strip()
    return shape, value


def check_ladders(markets):
    """Flag rungs whose prices contradict each other. Mutates `markets`."""
    groups = {}
    for market in markets:
        parts = _ladder_parts(market)
        if not parts or market.get("probability") is None:
            continue
        shape, value = parts
        key = (market.get("source"), market.get("event_title") or market.get("title"), shape)
        groups.setdefault(key, []).append((value, market))

    for (_, _, shape), rungs in groups.items():
        if len(rungs) < LADDER_MIN_RUNGS:
            continue
        rungs.sort(key=lambda pair: pair[0])
        ups = downs = 0
        worst = None
        for (lo_v, lo_m), (hi_v, hi_m) in zip(rungs, rungs[1:]):
            delta = hi_m["probability"] - lo_m["probability"]
            if delta > LADDER_TOLERANCE:
                ups += 1
                if worst is None or delta > worst[0]:
                    worst = (delta, lo_m, hi_m)
            elif delta < -LADDER_TOLERANCE:
                downs += 1
        if ups and downs and worst:
            _, lo_m, hi_m = worst
            note = (f"⚠️ Ladder inconsistent: {hi_m['outcome']} prints "
                    f"{hi_m['probability']:.1%} while {lo_m['outcome']} prints "
                    f"{lo_m['probability']:.1%} — some rungs are unquoted stubs")
            for _, market in rungs:
                if note not in market["flags"]:
                    market["flags"].append(note)


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
    if not markets:
        payload["verdict"] = "no_live_market"
        payload["note"] = ("No live market covers this question. Say so plainly; "
                           "do not substitute an estimate of your own.")
    return payload


def rank_key(market):
    """Relevance first, then money. A market that matches more of the
    question beats one that merely trades more."""
    return (-len(market.get("matched") or []), -_activity(market))


def _event_key(market):
    return (market.get("source"), market.get("event_title") or market.get("title"))


MAX_RUNGS_PER_EVENT = 20


def run_search(keywords, sources, limit, show_dropped=False):
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

    priced = drop_unpriced(results)
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

    per_source, trimmed = {}, []
    for key in order:
        source = key[0]
        if per_source.get(source, 0) >= limit:
            continue
        per_source[source] = per_source.get(source, 0) + 1
        trimmed.extend(grouped[key][:MAX_RUNGS_PER_EVENT])

    check_ladders(trimmed)
    trimmed.sort(key=rank_key)

    payload = build_search_payload(keywords, sources, errors, trimmed)
    payload["events"] = [
        {
            "source": key[0],
            "title": key[1],
            "outcomes_returned": len(grouped[key][:MAX_RUNGS_PER_EVENT]),
            "outcomes_total": len(grouped[key]),
            "url": grouped[key][0].get("url"),
        }
        for key in order if any(m in trimmed for m in grouped[key])
    ]
    if dropped:
        payload["dropped_as_irrelevant"] = len(dropped)
        if show_dropped:
            payload["dropped_examples"] = [
                {"source": m["source"], "title": m.get("title"),
                 "outcome": m.get("outcome"), "matched": m.get("matched", [])}
                for m in dropped[:25]
            ]
    return payload


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
    p_search.add_argument("--limit", type=int, default=4, help="events kept per venue")
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
        payload = run_search(args.keywords, sources, args.limit, args.show_dropped)
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
