# Reading a market price honestly

## The price is a probability, and that is all it is

A contract paying $1 if an event happens trades at $0.73 when the people risking
money collectively think it happens about 73% of the time. That is the whole
mechanism, and it is why these numbers are worth quoting: being wrong costs the
participants money, which is a discipline that pundits and polls do not have.

What it is not: a guarantee, a forecast from anyone in particular, or a number
that stays put. A 73% market is wrong roughly a quarter of the time — that is
what 73% means. Say "the market prices this at 73%", never "there is a 73%
chance".

## Volume decides whether the price means anything

This is the single most important judgment, and it is the one most easily
skipped because a thin market's price looks exactly like a deep market's price.

| Lifetime volume | What the number is worth |
|---|---|
| Over $1M | Real signal; many people paid to be right. |
| $50k – $1M | Usable, with the caveat stated. |
| Under $50k | Noise. Report it as noise or not at all. |
| Under $5k | One person's opinion wearing a percentage sign. |

Order-book depth matters alongside it. A market with $2M lifetime volume but
$300 of resting orders can be moved ten points by a single modest trade, so the
current price is nearly meaningless even though the history is not.

And check recency. A market with no trades in a week is quoting last week's
opinion; if news broke since, the price has not heard about it.

## Trend often beats level

"27%" alone is a weak answer. "27%, up from 12% a month ago" says something
happened. Both directions are informative:

- **Sharp move, high volume** — the market learned something. Worth finding out
  what.
- **Sharp move, thin volume** — probably one trader, not news.
- **Flat for weeks** — genuinely settled opinion, or nobody is paying attention.
  Volume separates the two.

## Cross-venue disagreement

Polymarket and Kalshi pricing the same question five or more points apart is
worth reporting, and usually has a mundane explanation before it has an
interesting one:

1. **The questions differ.** Overwhelmingly the most common cause. Check both
   resolution texts before calling it a disagreement — "cut by December" versus
   "cut at the December meeting" are different bets.
2. **Different crowds.** Kalshi is US-regulated and US-retail; Polymarket is
   crypto-native and global. On US domestic politics they can hold genuinely
   different views.
3. **Thin side.** If one venue's market is small, there is no disagreement — one
   of the two numbers is just noise.

Never average them. Report both with their volumes and let the reader weigh.

Manifold sitting far from a real-money venue is not disagreement at all. Play
money buys a different thing: there is no cost to being wrong, so prices drift
toward what is fun to believe.

## Where these markets are systematically off

Worth knowing, worth mentioning when relevant, not worth using to override the
price:

- **Longshot bias.** Low-probability outcomes trade persistently rich. A 3%
  market is more often 1% than 5%. Capital tied up for a year to earn 3 cents is
  unattractive, so the price does not fall as far as belief would suggest.
- **Long-dated markets compress.** Money locked up for two years costs the
  holder interest, which pushes far-future prices toward the middle.
- **Resolution risk is priced in.** Ambiguous criteria, or a resolver people do
  not trust, drags a market away from the true probability of the event itself.
- **Thin markets drift.** Without arbitrageurs, a stale price can sit wrong for
  weeks.

## Reading a ladder

A "how far" question is answered by a set of rungs at different thresholds, and
the shape of that set is the answer. Three things decide whether it means
anything.

**Anchor it.** A rung reading 52 percent says nothing until you know where the
price is now. Fetch the spot price and lead with it; otherwise the reader cannot
tell whether 52 percent is a routine wobble or a crisis.

**Know what "hit" means.** A touch ladder resolves if the level is reached at
any instant during the window — often on a one-minute low, sometimes only during
regular trading hours. A close ladder resolves on the closing print. The same
level prices very differently under the two rules, and titles rarely say which.

**Check it is internally consistent.** Whatever is true above 460 is also true
above 440, so a threshold ladder must be monotone. When it is not — and
Polymarket printed 92.4 percent for one and 90.0 percent for the other, on rungs
with no volume — the out-of-order rungs are unquoted maker stubs, not prices.
The script flags this. Treat the whole ladder as indicative when it fires.

Non-exclusive ladders (touch levels) legitimately sum past 100 percent, because
touching 540 and touching 520 are not alternatives. Mutually exclusive buckets
(price lands in one range) must sum to about 100; when they sum to 176, nobody
is quoting them.

## A note on liquidity figures

Resting-order depth is a snapshot of the book at the instant of the call, not a
stable property. On a thin market it can move severalfold within a minute — two
calls a moment apart returned 3,727 and 3,097 on the same rung. Quote it as
"depth right now", and do not treat a change between two calls as an error.

## What to say when there is no market

Say there is no market. That is a real answer and often the honest one — most
questions people ask have never been listed and never will be.

What not to do: pick a loosely related market and quote its number as if it
answered the question. If "will my company IPO this year" finds nothing, the
IPO-count market is not a substitute, and presenting it as one is worse than
saying nothing, because it wears the authority of a real price.

Naming the nearest markets and explaining exactly how they differ is useful. Any
probability you construct yourself is not a market probability and must never be
formatted like one.
