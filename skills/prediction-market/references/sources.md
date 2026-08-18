# Venue APIs, field mappings, and their silent failures

Every endpoint below is public, anonymous, and free. No key, no wallet, no
account. All of it was verified by direct request on 2026-08-18; where a
provider's own docs disagree, the observed behaviour is what is recorded here.

The recurring theme: **these APIs fail silently.** They return HTTP 200 with
data that is wrong for your purpose — settled markets presented like live ones,
renamed fields that now read as `null`, a mistyped filter that returns somebody
else's market. Nothing throws. Assume you are being handed something plausible
and wrong until you check.

---

## Polymarket

Base: `https://gamma-api.polymarket.com` (discovery),
`https://clob.polymarket.com` (prices), `https://data-api.polymarket.com` (holders)

### Search

```
GET /public-search?q={keywords}&limit_per_type=5&events_status=active
```

`events_status=active` is the only parameter that filters settled events —
`active=true&closed=false` is accepted and ignored. Even with it, results still
include events whose sub-markets have finished.

Search responses embed the full `markets` array, so discovery costs one request.

### Event detail

```
GET /events/{id}
GET /markets?condition_ids={conditionId}
```

`condition_ids` (snake_case) filters correctly. **`conditionIds` (camelCase) is
silently ignored and returns an unrelated market** — a query for a recession
market came back with "Xi Jinping out before 2027?" and HTTP 200. Always verify
the returned `conditionId` matches what you asked for.

`/events/{id}` carries `eventMetadata.context_description`: a Polymarket-written
news summary of why the market sits where it does. Useful, and free.

### Liveness — the trap that produces wrong answers

An event holds many sub-markets, and they die independently. Event 106884 held
five settled sub-markets alongside three trading ones. **The three live ones all
carried an `endDate` in the past.** Date tells you nothing.

| Signal | Meaning |
|---|---|
| `closed: true` | Dead. |
| `outcomePrices` exactly `["0", "1"]` | Dead, whatever `closed` says. |
| Price at 0.0095 | **Live.** A 0.95% market view, not a settlement. |
| `endDate` in the past | Nothing. Display it, never filter on it. |
| `volume24hr` null/0 and `volume1wk` 0 | Live but untraded; the price is stale. |

### Volume must be read per sub-market

Event-level `volume` sums every sub-market including the settled ones. That
event showed $3.1M at event level while the live sub-markets held a fraction of
it. Judging credibility on the event total overstates it by an order of
magnitude. Use `volumeNum` / `volume24hr` / `liquidityNum` on the sub-market.

### Outcome labels are not always Yes/No

`outcomes` sits beside `outcomePrices` and is easy to skip, because most markets
are `["Yes", "No"]` and the first price is the one you want. Some are not:
`Meta (META) Up or Down on August 19?` carries `["Up", "Down"]`. Reading
`outcomePrices[0]` as "probability of yes" turns a 55 percent chance the stock
*rises* into a 55 percent chance it *falls*. Always carry the label with the
price.

### Price history

```
GET https://clob.polymarket.com/prices-history?market={clobTokenId}&interval=1m&fidelity=1440
```

Takes a **CLOB token id** (from `clobTokenIds`), not a conditionId. `fidelity`
is in minutes; 1440 gives daily points.

Note the detail response for a single market ships only `oneMonthPriceChange` —
`oneDayPriceChange` and `oneWeekPriceChange` exist on the event-nested shape and
vanish here. Derive short-horizon deltas from this history instead.

### Holders

```
GET https://data-api.polymarket.com/holders?market={conditionId}&limit=500
```

Returns one list per outcome token; dedupe by `proxyWallet`. `limit` is a hard
cap — a list that comes back exactly full means the true count is higher, so
report "≥N". This is the closest thing to a participant count Polymarket
exposes; there is no such field.

---

## Kalshi

Base: `https://api.elections.kalshi.com`

### Search (undocumented, works)

```
GET /v1/search/series?query={keywords}
```

Not in the public docs, but it is the only keyword search Kalshi offers — the v2
API filters by ticker and series only. Treat it as liable to disappear and
degrade gracefully when it does.

Each result nests `markets` **with prices already attached**, so search alone is
enough for a first pass. Note the v1 shape still carries the old cent-denominated
names (`last_price: 15`) beside the new ones — v2 does not.

### Markets

```
GET /trade-api/v2/markets?event_ticker={ticker}
GET /trade-api/v2/markets/{ticker}
```

### Renamed fields — the silent one

`yes_bid`, `yes_ask`, `last_price`, `volume`, `open_interest` **no longer exist
in v2 responses.** Reading them yields `None`, so an implementation written to
the old docs produces an empty report and no error at all.

| Meaning | v2 field |
|---|---|
| Last traded price (= probability) | `last_price_dollars` |
| Best bid / ask | `yes_bid_dollars` / `yes_ask_dollars` |
| Resting size | `yes_bid_size_fp` / `yes_ask_size_fp` |
| Lifetime volume | `volume_fp` |
| 24h volume | `volume_24h_fp` |
| Open interest | `open_interest_fp` |
| Resolution rules | `rules_primary`, `rules_secondary` |

`_dollars` values are decimal strings ("0.7100"); `_fp` values are fixed-point
strings. Both need casting.

**`liquidity_dollars` is `0.0000` on every market**, including one with $4.3M
traded and $3.1M open interest. It is not a depth measure. Use open interest,
or the bid/ask sizes.

Contracts settle at $1, so contract counts and dollars are interchangeable.

### v1 search volume counts both sides

The nested `volume` on a v1 search result is exactly twice the v2
`volume_fp` for the same market, and the entry's own `total_volume` agrees
with the halved figure — the search endpoint reports both sides of each
trade. Since credibility thresholds key on volume, taking it at face value
makes the same market look twice as deep in search as in detail.

### Rules can arrive as an unrendered template

`rules_primary` sometimes comes back as `"If the price is above || Count ||
by || Date || at || Time ||"`. It parses fine and means nothing. Treat any
rules text containing `||` as absent.

### Price history lives on a series-scoped path

```
GET /trade-api/v2/series/{series}/markets/{ticker}/candlesticks
    ?start_ts=…&end_ts=…&period_interval=1440
```

The market-scoped spelling (`/markets/{ticker}/candlesticks`) 404s. `series`
is the ticker up to the first hyphen. Closes are in `price.close_dollars`,
with `price.mean_dollars` as a fallback on days with no close.

### Venue-level volume is not market-level volume

Search results carry `recent_volume` on the *event*. Attributing it to each
child market repeats Polymarket's event-total mistake — every sibling shows the
same figure. Keep it separate and label it as the event's.

---

## Manifold

```
GET https://api.manifold.markets/v0/search-markets?term={keywords}&limit=5
GET https://api.manifold.markets/v0/market/{id}
```

Play money. Useful for long-tail questions no real-money venue lists, and
useless as a price. Always label it.

`uniqueBettorCount` is a genuine participant count — the only venue that gives
one directly. Amounts are denominated in mana, not dollars; do not mix them into
a USD field. `MULTIPLE_CHOICE` markets carry no top-level `probability`.

---

## Metaculus

Best effort, and often unavailable.

The API went login-only in 2026 (`/api/posts/` and `/api2/questions/` both return
"available to authenticated users"), and the public site sits behind Cloudflare —
a plain request gets 403. Scraping the question list with a stealth browser works
and takes about 40 seconds.

Not a market: no money changes hands, so it reflects forecaster consensus rather
than a price. Valuable for long-horizon geopolitical and scientific questions
that no exchange will list. Never blend it into a market average.

---

## Adding a venue

Implement a `search_*` returning the normalized dict from `blank_market()`, and
register it in `SEARCHERS`. Before trusting any field, capture a real response
into `tests/fixtures/` and write the test against that — every trap on this page
was found by reading actual bytes, and none of them raised an exception.
