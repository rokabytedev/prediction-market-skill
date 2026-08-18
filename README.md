# prediction-market

**Ask a prediction market what the odds are, instead of guessing.**

An [Agent Skill](https://code.claude.com/docs/en/skills) that answers "how likely
is X?" with what people are actually betting — live prices from Polymarket and
Kalshi, with the volume and liquidity context you need to know whether the number
means anything.

Read-only. It never trades, holds no keys, and touches no wallet.

```
you: Is the US going into recession this year?

7.5% — Polymarket: US enters recession before end of 2026 (as of 18 Aug, 11:42 PT)

· Trend: -1pt over the week; down from 15.5% a month ago
· Size: $1.7M lifetime volume, $42k resting, 975 wallets holding → credible
· Resolves: NBER declares a recession, or two consecutive quarters of negative real GDP
· Cross-check: Kalshi 6%, Manifold (play money) 8.5% — all three agree
· https://polymarket.com/event/us-recession-by-end-of-2026
```

## Why this instead of just asking the model

Two reasons, and the second is the one that matters.

**Prediction markets are good at this.** People risking their own money on a
question tend to price it better than pundits, polls, or a language model's
recollection of the news. The price is a live probability, updated by whoever is
willing to bet against the current one.

**A price without context is worse than no price.** A market showing 73% on $400
of lifetime volume is one person's opinion wearing a percentage sign, and it
looks identical to a 73% backed by $10M. This skill always reports volume,
depth, recency and cross-venue agreement alongside the number, and flags the
ones you should not lean on.

And when no market covers the question — which is most of the time for anything
personal — **it says so instead of quietly substituting a related market or an
estimate of its own.**

## Install

### Claude Code, Codex, Cursor, and other CLI agents

As a plugin, which keeps it updatable:

```
/plugin marketplace add rokabytedev/prediction-market-skill
/plugin install prediction-market@prediction-market-skill
```

Or with the [`skills` CLI](https://github.com/vercel-labs/skills), which installs
into every agent you have:

```bash
npx skills add rokabytedev/prediction-market-skill
```

Or by hand:

```bash
git clone https://github.com/rokabytedev/prediction-market-skill
cd prediction-market-skill && make install     # → ~/.claude/skills/prediction-market/
```

### claude.ai (web)

claude.ai only accepts skills as a ZIP upload, so grab the packaged one:

**[Download prediction-market.zip](https://github.com/rokabytedev/prediction-market-skill/releases/latest/download/prediction-market.zip)**

Then **Settings → Capabilities → Skills → Upload skill**.

One extra step, or nothing will work: the skill sandbox has no outbound network
by default.

**Settings → Capabilities → Allow Network Egress → Domain allowlist → All domains**

Without it the skill still functions, falling back to Claude's own web-fetch
tool, but it loses every filter the script applies and gets noticeably worse.

## Using it

Just ask. The skill triggers on questions about whether something will happen:

- "Will the Fed cut rates in September?"
- "What are the odds Bitcoin ends the year above $150k?"
- "How likely is a recession next year?"
- "美联储九月会降息吗" · "AI 泡沫会破吗"

It answers in whatever language you asked in.

### Or drive the script directly

```bash
cd skills/prediction-market

python3 scripts/pm_query.py search "US recession 2026" "recession this year"
python3 scripts/pm_query.py detail polymarket 0xfdc73f10...
python3 scripts/pm_query.py detail kalshi KXRECSSNBER-26
python3 scripts/pm_query.py compare kalshi:KXRECSSNBER-26 polymarket:0xfdc73f10...

python3 scripts/pm_query.py search "..." --sources polymarket,kalshi --limit 6
```

Python 3.9+, standard library only. No pip install, no API keys, no accounts.

## Sources

| Venue | Money at stake | Access | Weight |
|---|---|---|---|
| [Polymarket](https://polymarket.com) | Real (USDC on Polygon) | Public API, no key | Primary |
| [Kalshi](https://kalshi.com) | Real (USD, CFTC-regulated) | Public API, no key | Primary |
| [Manifold](https://manifold.markets) | Play money | Public API, no key | Long tail, always labelled |
| [Metaculus](https://metaculus.com) | None (forecaster consensus) | Scraped, opt-in | Best effort |

Every read endpoint used here is free and anonymous.

Polymarket and Kalshi cover different ground — Kalshi is stronger on US economic
releases, weather and domestic policy; Polymarket on crypto, geopolitics and
anything global — so querying both roughly doubles hit rate, and the gap between
them on a shared question is itself a signal.

Metaculus is off by default: its API went login-only in 2026, so it needs the
[`scrapling`](https://github.com/D4Vinci/Scrapling) CLI and takes about 40
seconds. Opt in with `--sources metaculus`.

## How it decides whether a number is trustworthy

Thresholds are in the code, not left to the model's judgement:

| Condition | What you see |
|---|---|
| Lifetime volume < $50k | ⚠️ Thin market — this number is noise |
| Resting orders < $5k | ⚠️ Shallow book — one sizeable order moves the price |
| No trades recently | ⚠️ No recent trading — the price may be stale |
| Price below 2% or above 98% | Market treats this as all but settled |
| Source is Manifold | ⚠️ Play-money market — indicative only |
| Source is Metaculus | Not a market — forecaster consensus |
| Real-money venues differ by >5pt | Flagged for a closer look |

Settled markets are dropped entirely rather than flagged — their final price of
0 or 1 is indistinguishable from a live probability, and that mistake is silent.

## What it refuses to do

**Guess.** If nothing live matches, the script returns
`"verdict": "no_live_market"` and the skill reports that. It will name the
nearest related markets and explain how they differ, but it will not produce a
probability of its own. A number that looks like a market price but isn't is
worse than no answer.

**Trade.** No keys, no signing, no order placement, by design — a skill that can
run in a cloud sandbox has no business holding a private key. If you want to act
on what you find, open the market yourself.

**Give investment advice.** It reports prices; what you do with them is yours.

## Why the code is so defensive

Every venue API returns HTTP 200 with data that is wrong for this purpose.
Nothing throws. Each of these was found by measuring the live APIs, and each is
covered by a test:

- **Search is dominated by settled markets.** "Fed September rate cut" returns
  2024 markets whose final price reads exactly like a live probability.
- **Sub-markets die independently of their event**, and live ones can carry an
  `endDate` in the past — one event had three actively trading sub-markets whose
  end dates had all passed. Filtering by date both keeps dead markets and drops
  live ones.
- **Event-level volume sums in the settled children**, overstating a live
  sub-market's credibility by an order of magnitude.
- **Kalshi renamed every money field.** `yes_bid`, `last_price`, `volume` and
  `open_interest` now read `null`, so an implementation written against the old
  docs reports nothing and raises nothing.
- **Kalshi's `liquidity_dollars` is 0.0000 on every market**, including ones with
  millions traded. It is not a depth measure.
- **Polymarket's `conditionIds` filter is silently ignored** — camelCase instead
  of `condition_ids` returns an unrelated market, with a 200.
- **Venue search is fuzzy and always returns something.** "Will I get promoted
  next year" matched the Los Angeles mayoral race; "will my cat learn to play
  piano" matched Super Bowl halftime performers.

`skills/prediction-market/references/sources.md` documents all of it, endpoint by
endpoint.

## Tests

```bash
make test         # 56 unit tests, offline, ~0.02s
make test-live    # 9 end-to-end tests against the live APIs
```

Unit tests run against **captured real API responses**, so an upstream schema
change breaks a test instead of quietly producing a wrong answer.

The live suite's negative controls are the important ones: questions no venue
lists ("will I get promoted next year") must come back empty. The first
implementation failed that check, which is why it is now permanent — along with
a guard that fails if the venues stop returning fuzzy matches at all, so the
controls can never pass vacuously.

## Layout

```
skills/prediction-market/
├── SKILL.md                 # the instructions Claude reads
├── scripts/pm_query.py      # stdlib-only multi-venue client
└── references/
    ├── sources.md           # endpoints, field maps, silent failures
    └── interpreting.md      # how to read a price honestly
tests/                       # unit tests + captured fixtures + live suite
docs/design-notes.md         # why it is built this way
.claude-plugin/              # plugin manifests
```

## Adding a venue

1. Write a `search_*` function returning the normalized dict from
   `blank_market()`, and register it in `SEARCHERS`.
2. Capture a real response into `tests/fixtures/` and write the test against
   those bytes. Do not trust the venue's documentation — every trap listed above
   was found by reading actual responses, and none of them raised an exception.
3. If the venue is play money or not a market, make `credibility_flags` say so.

## Disclaimer

Not financial advice. Prediction market prices are probabilities, not
guarantees — a 73% market is wrong about a quarter of the time, and that is what
73% means. Prediction markets are also restricted or unavailable in some
jurisdictions; this tool only reads public data and takes no position on whether
you may trade on them.

## License

MIT
