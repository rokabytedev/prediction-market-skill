---
name: prediction-market
description: Look up what real-money prediction markets (Polymarket, Kalshi, plus Manifold and Metaculus as weaker signals) currently price a future event at, including probability, trend, volume, participants and a credibility check. Use whenever someone asks whether some public event will happen, how likely it is, what the odds are, or what the market thinks — "will X happen", "what are the chances", "odds of", "how likely is", "会不会发生", "有多大可能", "概率多大", "予測市場". Read-only; it never trades. When no live market matches the question it says so rather than inventing a number.
license: MIT
---

# Prediction Market Lookup

Answer "how likely is X?" with what people are actually betting, not with a guess.

**This skill is a lookup tool, not a forecaster.** If no live market covers the
question, say so. Never present your own estimate as a market probability, and
never fill a gap with a number you reasoned your way to.

## Run it

```bash
python3 scripts/pm_query.py search "keyword group one" "keyword group two"
python3 scripts/pm_query.py detail polymarket <conditionId>
python3 scripts/pm_query.py detail kalshi <ticker>
python3 scripts/pm_query.py compare kalshi:<ticker> polymarket:<conditionId>
```

Stdlib Python only — no install step. Paths are relative to this skill directory.

## Workflow

### 1. Turn the question into English keywords

Every venue lists in English, so a question in any other language finds nothing
until you translate it. Write **two or three** keyword groups covering different
phrasings, and use the words a market would use, not the words the user used:

| User asks | Search |
|---|---|
| Will the Fed cut in September? | `"Fed rate cut September"` `"FOMC September decision"` |
| 美联储九月会降息吗 | `"Fed rate cut September"` `"FOMC September decision"` |
| Is the AI bubble going to pop? | `"AI bubble burst"` `"AI bubble pop"` |
| 美国明年会衰退吗 | `"US recession 2027"` `"recession next year"` |

### 2. Search

```bash
python3 scripts/pm_query.py search "US recession 2026" "recession this year"
```

Returns live markets only. Already-settled ones are dropped, and results that
merely share a generic word with the question are filtered out — the venues'
search endpoints are fuzzy and will happily answer "will I get promoted?" with
the Los Angeles mayoral race.

### 3. Check the resolution criteria before believing a match

**This is the step that decides whether the answer is right.** Titles mislead:
"Fed rate cut by December" can mean the December meeting or any cut before
December, and those are different questions with different prices. Read `rules`
(Kalshi) or `rules` / the description (Polymarket) and confirm it resolves on
the thing the user actually asked about.

If the closest market answers a *related but different* question, say that
explicitly rather than quietly substituting it.

### 4. Pull detail on the market you picked

`detail` adds the 30-day probability history, holder counts, order-book depth
and the full resolution text. Run it before writing the answer — search results
carry no resolution text and no trend.

Comparing venues? Use `compare`, and only on markets you have confirmed ask the
same question. The script deliberately refuses to compute a cross-venue spread
during search, because comparing "Cut 25bps" on one venue against "No change"
on another manufactures a meaningless 70-point "disagreement".

### 5. Write the answer

Conclusion first, roughly 150 words, no preamble:

```
**7.5%** — Polymarket: US enters recession before end of 2026 (as of 8/18 11:42 PT)

· Trend: -1pt over the week; down from 15.5% a month ago
· Size: $1.7M lifetime volume, $42k resting, 975 wallets holding → credible
· Resolves: NBER declares a recession, or two consecutive quarters of negative real GDP
· Cross-check: Kalshi 6%, Manifold (play money) 8.5% — all three agree
· https://polymarket.com/event/us-recession-by-end-of-2026
```

Rules for that block:

- **Write it in the user's language.** The script emits English labels and
  flags; translate them. Never drop a ⚠️ because the answer reads better
  without it — those flags are the difference between a number worth acting on
  and noise, and every one the script returns must appear in your answer.
- Report the probability as the market's view, never as yours. "The market
  prices this at 73%", not "there is a 73% chance".
- Multi-outcome events (who wins X): list the top three outcomes with prices.
- Manifold is play money and Metaculus is not a market — label both, and never
  lead with them when a real-money venue has the same question.
- Give the market link on its own line, plain text.

## When nothing matches

The script returns `"verdict": "no_live_market"`. Before reporting that, **try
once more with different wording** — a question about Taiwan might be listed as
"China invade Taiwan", "Taiwan blockade" or "China Taiwan military action".

If the retry is also empty, say plainly that no prediction market covers this,
and stop. You may name the nearest related markets and explain how they differ.
You may not produce a probability.

Most personal questions ("will I get promoted", "will my offer be accepted")
have no market and never will. That is a fine answer.

## Platform notes

**Claude Code / Codex / Cursor** — works as-is.

**claude.ai** — the skill sandbox blocks outbound network by default, so the
script will fail with a connection error. Tell the user once:

> Settings → Capabilities → Allow Network Egress → Domain allowlist → All domains

If they would rather not enable it, fall back to fetching these URLs with your
own web-fetch tool and reading the raw JSON yourself:

- `https://gamma-api.polymarket.com/public-search?q=KEYWORD&limit_per_type=5&events_status=active`
- `https://api.elections.kalshi.com/v1/search/series?query=KEYWORD`

The fallback is worse: responses are large, and every filter the script applies
(settled markets, stale prices, relevance, credibility) you must then apply by
hand. `references/sources.md` lists the traps.

**Metaculus** is off by default. It needs the `scrapling` CLI on PATH (or
`PREDICTION_MARKET_SCRAPLING` pointing at the binary), takes ~40s, and is
skipped silently when unavailable. Opt in with `--sources metaculus`.

## Deeper reference

- `references/sources.md` — endpoints, field mappings, and the silent failures
  each venue's API is prone to. Read before changing the script or hand-fetching.
- `references/interpreting.md` — how to read a probability, when volume makes a
  price meaningless, and what cross-venue disagreement means.

## Never

- Place, sign, or prepare a trade. This skill is read-only and holds no keys.
- Give investment advice.
- Present a settled market's final price as a current probability.
- Report a number without its credibility flags.
