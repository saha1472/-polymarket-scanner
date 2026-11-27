# GPU price markets: a separate paper test

Written 27 November 2026, before this test had recorded anything. It places no orders and needs
no keys. It is separate from the main bot's rewards record and writes only
`gpu_paper_state.json` and `gpu_paper_log*.jsonl`.

Already seen when these rules were chosen: about one day of market-wide data (26-27 November)
suggesting the average maker on these markets is between break-even and losing after rewards.
No result from this test had been seen. Before merging, a throwaway dry run (two runs a few
minutes apart, no fills) checked the mechanics; no pass-mark line was changed after it.

## The question

Does a small resting quote on Polymarket's monthly "average GPU rental price" markets earn more
in liquidity rewards than it loses when it is filled?

## What is simulated

- **Markets:** chosen on the first run, then fixed. Monthly-average GPU markets with a reward
  pool, minimum size 50 shares or less, midpoint 0.15-0.85, spread inside the reward band,
  other makers resting there, a known fee, and at least 30 days left. Two groups, judged
  separately: **busy** (pool $100 a day or more) and **quiet** (under $100). In each group:
  one market per event, highest pool first, then nearest end date, then midpoint closest to
  0.50; five markets at most.
- **Quote:** at every run (about 15 minutes apart) a 50-share bid and ask level with the best
  qualifying prices. No quote while the midpoint is outside 0.15-0.85, the pool is gone, the
  minimum size is above 50, or either side would fall outside the reward band.
- **Reward:** the low estimate of our share times the pool's current daily rate, taking the
  lower of the estimate when the quote was placed and at the next run. It stops at the first
  fill, and is zero if the next run is more than 30 minutes later.
- **$1 rule:** a market's reward for one UTC day counts only if it reaches $1 (reward earned
  across midnight is split between the two days). The docs say the minimum payout is $1 without
  saying per market or per account; this is the stricter reading.
- **Fills:** from the public trade feed (each resting order's fill at its own price), from the
  time the quote was placed to the next run, 30 minutes at most. Two counts:
  - **sure:** only trades at a price worse than ours (the market traded through our quote);
  - **possible:** every trade at our price or through it.
- **After a fill:** the position is closed at the next run against the book as it then is,
  walking the depth and paying the market's taker fee. Nothing is carried forward. Shares the
  book cannot absorb count as a total loss.
- **A window that cannot be read** (no book, or trade rows that do not add up) is retried for
  24 hours, then counted as the worst case: one side filled and lost in full.

## Pass mark

The test period is the 14 full UTC days that start at the first midnight after the first
record. It is judged one day after it ends, so that late windows are in. Each group is judged
on its own, and every line must hold under **both** fill counts.

**Enough data first:** runs cover at least 90% of the period, and there are at least 30 quote
windows with a sure fill. If not, the result is **NOT ENOUGH DATA** and the group is read once
more over days 1-28. If it is still short then, the answer is "not proven" and the test ends.

**PASS** needs all of:

1. net (counted reward plus fill result) above zero;
2. net above zero in each half of the period;
3. net still above zero without the best market, and without the best day;
4. net still above zero if every reward were only half the estimate (halved before the $1 rule).

Anything else is **FAIL**. The verdict is read once. Numbers seen earlier are interim and decide
nothing. These rules do not change after the first record; a change means a new test with a
new log. Recording stops by itself 31 days after the first run.

## What a pass would not mean

- The reward share is estimated, never measured. The real figure could be below half of it.
- A real order changes the book others react to. Our place in the queue is unknown, which is
  why fills are counted two ways.
- Quotes here are replaced only every 15 minutes or so. A real bot would be faster; stale
  quotes get hit more, while the reward assumes the quote stayed in the band in between.
- The chosen markets will mostly be months of the same GPU, which move together, so five
  markets are not five independent tests.
- One 50-share quote on both sides locks about $49-50: more than a $50 test budget with $2
  held back. The paper capital here is about $250 per group.
- Passing is not permission to trade. Country restrictions and the project's other go/no-go
  gates still apply.

## Reading the results

```
git pull
python gpu_paper.py --report
```
