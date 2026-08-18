---
name: prediction-market
description: Look up what real-money prediction markets (Polymarket, Kalshi, plus Manifold and Metaculus as weaker signals) currently price a future event at, including probability, trend, volume, participants and a credibility check. Use whenever someone asks whether some public event will happen, how likely it is, what the odds are, where a stock or coin will trade, or what the market thinks — "will X happen", "what are the chances", "odds of", "how likely is", "how far will it fall", "会不会发生", "有多大可能", "概率多大", "予測市場". Read-only; it never trades. When no live market matches the question it says so rather than inventing a number.
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
python3 scripts/pm_query.py spot META          # underlying price, to anchor a ladder
```

Stdlib Python only — no install step. Paths are relative to this skill directory.

## Workflow

### 1. Turn the question into English keywords

Every venue lists in English, so a question in any other language finds nothing
until you translate it. Write **two or three** keyword groups covering different
phrasings. Groups are scored independently, so an extra phrasing can only help.

**Always include one group that is the bare subject on its own** — the company,
person, coin or country, with no qualifiers. Venue titles rarely contain the
words a person would use ("stock", "price", "fall"), and those words are common
enough across markets that they carry no signal. The bare name is what matches.

| User asks | Search |
|---|---|
| Will the Fed cut in September? | `"Fed"` `"Fed rate cut September"` |
| 美联储九月会降息吗 | `"Fed"` `"Fed rate cut September"` |
| Will Meta keep falling? | `"Meta"` `"Meta Platforms"` `"Meta up or down"` |
| Is the AI bubble going to pop? | `"AI bubble"` `"AI bubble burst"` |
| 美国明年会衰退吗 | `"recession"` `"US recession 2027"` |

### 2. Search

```bash
python3 scripts/pm_query.py search "Meta" "Meta Platforms" "Meta up or down"
```

Returns live markets only, grouped under an `events` index that tells you how
many outcomes each event has. Settled markets are dropped, and results sharing
only a generic word with the question are filtered out.

`verdict` is `found`, `no_live_market`, or `sources_unavailable` — the last
means nothing could be queried, which is not the same as nothing existing.

Coverage is capped at `--limit` events per venue (default 8). When more
matched, `limit_note` and `events_not_returned` say so; raise `--limit` rather
than assuming you saw everything. `--show-dropped` adds `dropped_examples`,
the markets the relevance gate rejected. Check both before concluding a market
does not exist.

Quote `display_title`, not `title`: in search output every row of an event
shares the event's title, so "What will META hit in August 2026?" plus 50.5%
does not say whether that is a rise or a fall, and "Nobel Peace Prize winner"
plus 30% names nobody. `display_title` folds in the row's own `outcome`.

### 3. Check the resolution criteria before believing a match

**This is the step that decides whether the answer is right.** Titles mislead:
"Fed rate cut by December" can mean the December meeting or any cut before
December, and those are different questions with different prices. A market
titled "What will META hit in August" is not about the closing price — it
resolves on whether any one-minute candle *touches* the level intraday.

Read `rules` and confirm it resolves on the thing the user actually asked about.
Kalshi search results carry no rules — run `detail` to get them. Manifold never
has any, and a market flagged "no resolution text" cannot be verified at all,
so it must not carry an answer on its own.

Check the window too, not just the level: a Polymarket ladder titled "in 2026"
can have opened in late 2025, so part of what it covers has already happened.
If the closest market answers a *related but different* question, say that
explicitly rather than quietly substituting it.

**A candidate surviving the search is not evidence that a market exists.** The
relevance gate is deliberately a little loose so it does not hide real markets,
so a thin coincidence gets through now and then — a question about your
daughter's college application can surface a football game against the same
university, and one about your own company can surface a market whose listed
outcome merely ends in "Company". Each candidate carries `matched`, the words it
matched on; when that is one or two incidental words, be suspicious.

If every candidate fails this check, the answer is **no market**, exactly as if
the search had come back empty. Do not soften it into "here is a related
market's number".

### 4. Pull detail on the market you picked

`detail` adds the 30-day probability history, holder counts, order-book depth,
the full resolution text and a `fetched_at` timestamp. Run it before writing the
answer — search results carry no resolution text and no trend.

Comparing venues? Use `compare`, and only on markets you have confirmed ask the
same question. It returns `spread_pp`, `agree`, and `caveats`. **`agree` is `null` when
agreement could not be verified** — a missing end date, missing rules, or two
sides resolving on different events (confirming an IPO is not completing one).
Null is not agreement; report the spread and the caveat, not a match. The script deliberately refuses to compute a cross-venue spread
during search, because comparing "Cut 25bps" on one venue against "No change" on
another manufactures a meaningless 70-point "disagreement".

### 5. Price-level questions need an anchor

"How far will it fall?" is answered by a **ladder** — a set of rungs at
different thresholds. A rung reading 52 percent is meaningless until you know
where the price is now, so run `spot TICKER` first and lead with it.

Report the ladder as a ladder: several rungs in order, so the reader sees the
distribution. Do not truncate it to three rows — the shape is the answer. Say
whether the rungs are *touch* (any point intraday) or *close* levels; they price
very differently.

Rungs come back grouped by event and ordered by threshold, with `↑` and `↓`
sides kept apart. Three checks run over them, and each flag names the rungs it
is about:

- **Ladder inconsistent** — rungs within one event contradict each other. It is
  stamped on the direction group it belongs to, so a clean `↓` side stays clean
  while a broken `↑` side is called out. Those rungs are unquoted stubs rather
  than prices.
- **Cross-market inconsistency** — the same underlying priced out of order
  across two events, e.g. touching 170k dearer than touching 150k.
- **Window inconsistent** — a shorter window priced above a longer one that
  contains it.
- **Distribution incomplete / incoherent** — mutually exclusive outcomes that
  sum to well under 100% (some are missing) or above it (the quotes contradict
  each other). This fires on any field of alternatives, prices and names alike.
- **End date already passed** — the market is past its date and may be awaiting
  settlement rather than showing a live view.

A flag makes those rungs indicative at best. Say which rungs it applies to
rather than discrediting the whole answer.

### 6. Write the answer

Conclusion first, no preamble. Roughly 150 words for a single market; a ladder
or a two-part question will run longer, and that is correct — never drop rungs
or flags to hit a word count.

```
7.5% — Polymarket: US enters recession before end of 2026 (as of 18 Aug, 11:42 PT)

· Trend: -1pt over the week; down from 15.5% a month ago
· Size: 1.7M USD lifetime volume, 42k USD resting, 975 wallets holding → credible
· Resolves: NBER declares a recession, or two consecutive quarters of negative real GDP
· Cross-check: Kalshi 6%, Manifold (play money) 8.5% — all three agree
· https://polymarket.com/event/us-recession-by-end-of-2026
```

Rules for that block:

- **Never quote a price without saying which outcome it belongs to.** Every
  market carries `probability_of` and `outcome_prices`. A market with outcomes
  Up and Down priced at 0.55 means *Up* is 55 percent — reporting a bare 55
  percent on a question phrased "will it fall" states the opposite of the
  market's view. Check the label every time.
- **Write it in the user's language.** The script emits English labels and
  flags; translate them. Never drop a ⚠️ because the answer reads better
  without it — every flag the script returns must appear in your answer.
- Report the probability as the market's view, never as yours. "The market
  prices this at 73%", not "there is a 73% chance".
- **Multi-outcome events (who wins X): lead with the field's sum, not with a
  name.** The `events` index carries `outcomes_sum`. When it is far below 100%
  the rest of the probability sits on outcomes you were never shown, so "the
  favourite is X at 8.5%" is false — X is only the highest of what came back.
  Say how much is unaccounted for, then give the top few **by probability**
  (the script now orders them that way). When the sum is above 100% the quotes
  contradict each other and none of them is worth much.
  Ladders are different — show the rungs in order, per step 5.
- A price under 2% is flagged "priced as a long shot", not settled. On a ladder
  tail that is ordinary, so report it as the market's view of a remote outcome.
- `possibly_truncated` on an event means it filled the per-event cap, so there
  are probably more outcomes than you can see. Raise `--limit` or say so.
- Manifold is play money and Metaculus is not a market — label both, and never
  lead with them when a real-money venue has the same question.
- Give the market link on its own line, plain text.

### 7. Multi-part questions

"Will it fall, and how far?" is two questions. Answer them separately and say
which part the market actually covers.

Watch for the two parts resolving on **different events**. A "when will X IPO"
ladder can resolve on the company *announcing* an offering while the "will X
IPO by date" market resolves on it *completing* one — months apart, and not
comparable. Report them as separate answers; never present one as the other's
cross-check. It is common for the level question to
have a ladder while the direction question has only a thin daily up/down market,
or for the near term to be priced and the horizon the user asked about to have
no market at all. Say so plainly instead of stretching one answer over both.

## When nothing matches

The script returns `"verdict": "no_live_market"`. Before reporting that:

1. Retry with the **bare subject alone** if you have not already.
2. Try the formal name — "Meta Platforms" rather than "Meta stock", "Alphabet"
   rather than "Google".
3. Run `--show-dropped` to see whether the market is there and being filtered.

If all three come back empty, say plainly that no prediction market covers this,
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
(settled markets, stale prices, relevance, ladder integrity, credibility) you
must then apply by hand. `references/sources.md` lists the traps.

**Metaculus** is off by default. It needs the `scrapling` CLI on PATH (or
`PREDICTION_MARKET_SCRAPLING` pointing at the binary) and takes ~40s. Add it to
the full list — `--sources polymarket,kalshi,manifold,metaculus` — because
`--sources` **replaces** the default rather than extending it. Passing
`--sources metaculus` alone queries nothing else, and on a machine without
scrapling that returns `sources_unavailable`.

## Deeper reference

- `references/sources.md` — endpoints, field mappings, and the silent failures
  each venue's API is prone to. Read before changing the script or hand-fetching.
- `references/interpreting.md` — how to read a probability, when volume makes a
  price meaningless, how to read a ladder, and what cross-venue disagreement means.

## Never

- Place, sign, or prepare a trade. This skill is read-only and holds no keys.
- Give investment advice.
- Present a settled market's final price as a current probability.
- Report a number without its credibility flags, or a price without its outcome label.
