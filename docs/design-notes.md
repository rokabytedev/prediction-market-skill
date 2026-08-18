# Design notes

Why this skill is shaped the way it is. Field-level API detail lives in
`skills/prediction-market/references/sources.md`; this page is about the
decisions.

## The premise

Prediction markets are unusually good at pricing public events, and unusually
easy to misread. Both halves drive the design: fetch the price, and refuse to
present it without the context that says whether it is worth anything.

## It is a lookup tool, not a forecaster

The single most consequential decision. When no market covers a question, the
options were:

1. Report that no market exists.
2. Find loosely related markets and reason to an estimate.
3. Fall back to ordinary web research.

**Chosen: (1).** Options 2 and 3 both end with a number that the user will read
as a market price. Once the skill is willing to produce a probability from its
own reasoning, every answer it gives becomes ambiguous — the reader can no
longer tell which numbers came from people betting money and which came from a
language model, and the whole reason to consult a market evaporates.

The cost is real: most personal questions return nothing. That is the correct
outcome, and saying so is a useful answer.

## Fetching: a script, not the agent's web tools

Two reasons.

**Context.** A single Polymarket event response runs to tens of kilobytes.
Four venues of raw JSON in the conversation crowds out the actual work.

**Fidelity.** Some agents' fetch tools summarize a page through a small model
before the main model sees it. Having a summarizer restate probability figures
is not acceptable for numbers presented as market prices. The script reads the
bytes and extracts fields.

The script uses **only the Python standard library** so it runs unchanged in a
locked-down sandbox with no pip access — that constraint is what makes one
artifact work on both a developer machine and claude.ai.

## Two-stage query

`search` returns compact candidates; `detail` returns the deep record for one
market. Splitting them matters because the middle step is human judgement the
script cannot do: **checking that the resolution criteria actually match the
question asked.** "Fed rate cut by December" and "Fed rate cut at the December
meeting" are different bets with different prices and near-identical titles.

So search stays cheap and broad, the model picks, and only then does the
expensive call happen.

## The relevance gate lives in code

Every venue's search is fuzzy and never returns nothing. Asked "will I get
promoted next year", they offered the Los Angeles mayoral race; asked about a
cat learning piano, Super Bowl halftime performers.

Instructing the model to ignore irrelevant results is not enough — an
instruction is something a model can reason its way around, and the failure mode
is exactly the one this skill exists to prevent. So the gate is code: a market
must share two content words with the query, or one distinctive word (six or
more characters), after stopword removal and light stemming.

The thresholds were tuned against both directions. Requiring two matches alone
dropped Kalshi's "Recession this year?" for a query of "US recession 2026" —
a false negative is its own wrong answer, and the distinctive-word rule exists
to fix it. Stemming exists because Kalshi lists "Will Trump be impeached?" for
a question phrased "Trump impeachment".

## Credibility thresholds are constants, not judgement

`MIN_VOLUME = 50_000`, `MIN_LIQUIDITY = 5_000`, near-certainty at 2%. Hard-coded
so the same market gets the same warning every time, regardless of how the
question was phrased or how confident the surrounding conversation felt.

The skill instructions state that every flag the script emits must appear in the
answer. Dropping a ⚠️ because the prose reads better without it is the failure
this guards against.

## Cross-venue divergence is computed only on request

An early version compared the highest and lowest probability across all search
results and reported the spread. On a Fed query this produced "70 point
disagreement" — by comparing *Cut 25bps* on one venue against *No change* on
another. Two different questions, one meaningless number.

Nothing at search time establishes that two rows describe the same outcome, so
`search` no longer computes divergence at all. The `compare` subcommand takes
markets the caller has explicitly matched.

## Output is English; answers are not

The script emits English labels and flags. The skill instructions tell the model
to render them in the user's language. This keeps the tool portable while
letting it answer a Chinese question in Chinese.

## Testing against captured bytes

Unit tests load real API responses saved to `tests/fixtures/`, not hand-written
JSON. Every trap the code defends against was found by reading actual responses,
and none of them raised an exception — a hand-written fixture would have
encoded the schema as documented rather than as served, and tested nothing.

The live suite exists for the negative controls. A tool that answers "how likely
is X?" is only trustworthy if it reliably declines when no market exists, and
that check failed on the first implementation. It also carries a guard against
vacuous passes: if the venues ever stop returning fuzzy matches, the guard fails
rather than letting the controls pass for the wrong reason.
