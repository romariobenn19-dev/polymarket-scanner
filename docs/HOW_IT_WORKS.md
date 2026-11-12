# How the rewards bot works (version E)

Plain-language description of exactly what the bot does, step by step, with every number it uses.
Use it to check each rule and spot what could be better. Everything here is **paper trading**:
no account, no keys, no real orders. The code is `rewards_bot_e.py` (built on `rewards_bot_d.py`
and `rewards_bot.py`).

---

## Version F (rewards_bot_f.py): what changes from E

Sections 1–7 below describe version E. Version F keeps all of it except:

| # | Rule in F | Replaces in E |
|---|---|---|
| 1 | **Realistic paper fills.** An order remembers how many shares were already waiting at its price. Trades at that price use up that queue first; it fills only when the queue ahead is gone, a trade happens at a worse price, or the other side crosses it. People ahead cancelling moves it up. | Any trade at the order's price counted as a fill. |
| 2 | **$1 payout minimum.** Polymarket does not pay reward amounts under $1. A market is only used if it is expected to earn **$1.25+/day**; if minimum-size orders can't reach that, size goes up (2×, 3×, 4×) using at most 80% of the budget in that market. | No minimum. |
| 3 | **Adjusted midpoint.** Book levels smaller than the minimum reward size are ignored for the midpoint, microprice and other traders' scores (Polymarket scores from this size-cutoff-adjusted midpoint). | Raw best prices. |
| 4 | **No near-worthless orders.** A new order needs queue cover within **80%** of the reward zone, else that side is not quoted. | Fell back to the far edge, scoring ~0 with full fill risk. |
| 5 | **One market per event.** Never two markets from the same event, and none from an event where shares are already held. | Allowed (e.g. two NVIDIA CEO markets). |
| 6 | **Exit ramp.** Stuck shares get a waiting sell order at break-even (price paid minus rewards already earned in that market); after 90 min it steps 1 tick lower every 15 min, never below best bid + 1 tick. The forced sale at 3 h stays as the last resort. The exit order keeps working during pauses. | Half distance, then forced sale at 3 h. |
| 7 | **Losses judged against earnings.** A market with negative net result gets orders 0.10 of the zone further out (up to 0.30). It is banned for 24 h only after losing more than max(expected daily rewards, $0.30). | Banned after $0.30. |
| 8 | **Minute-level risk.** Market choice uses the last day of minute-by-minute prices instead of a week of hourly prices, plus the cost of one forced sale a day at the market's own taker fee. Finer price steps score better automatically because orders land closer to the target distance. | Hourly prices, no fee. |

**First result:** with these rules, on the afternoon of 12 Nov 2026 **no market qualified** with $50.
Calm markets paid under $1/day at the sizes $50 allows; the one that cleared $1.25 (a TV contest)
had 16 one-minute jumps of 3¢+ in a day, up to 16.5¢, which hourly data had hidden. Applying the $1
minimum to night 1's results: A, B, C and D would have been paid **$0** if the minimum applies per
market per day (only B's $1.44 for 12 Nov if it applies to the daily total).

---

## 1. Where the money comes from

Polymarket pays a daily amount on many markets ("liquidity rewards") to people who keep **buy and
sell orders waiting close to the current price**. The bot tries to collect part of that.

- **Rewards (income):** paid every day at midnight UTC, split between everyone quoting in that market.
- **Stuck shares (risk):** when the price moves through one of the bot's orders, the order fills and
  the bot now owns shares bought at the old price. If the price keeps going, that is a loss.

Last night's test showed rewards of about **$2–3.60 a day on $50**, while gains and losses on stuck
shares were several times larger and mostly luck. Version E is built around controlling the second part.

### How Polymarket splits the rewards (as the bot models it)

- Every minute Polymarket looks at all waiting orders within the market's **max spread** (`v`, usually
  4.5¢) of the **midpoint** (halfway between best buy and best sell price).
- Each order scores `((v − distance) / v)² × size`. An order right at the midpoint scores its full size;
  halfway to the edge it scores a quarter; at the edge, nothing.
- Buy-side and sell-side scores are combined. Having **both sides counts fully**, while one side alone
  counts a third (`c = 3`; the official settings page should confirm this value).
- Your share of the day's pool = your score ÷ everyone's score.

**Uncertain:** the bot cannot see other traders' individual orders, only the total at each price, so
its reward share is an estimate. Real payouts may be lower.

---

## 2. Choosing markets (every 60 minutes)

1. Download all open events with at least $500 of liquidity (about 10,000 events, 120,000 markets).
2. Keep only markets that:
   - pay rewards and are accepting orders;
   - have a current price between **15¢ and 85¢** (near 0 or 100 prices jump at the end);
   - resolve **7+ days** from now;
   - traded at least **$500 in the last 24 h**;
   - moved **≤ 1¢ in the last hour and ≤ 3¢ in the last day**;
   - are **not** about scheduled data or fast prices: CPI, PCE, inflation, Fed, interest rates, bps,
     GDP, unemployment, payrolls, jobs report, temperature, Bitcoin/Ethereum/Solana, "price of",
     "above $", "below $", "close above/below", "market cap";
   - are **not** live sports or short crypto ("vs.", "O/U", "Spread:", "Up or Down", "Handicap");
   - need no more than **$25** to quote at the minimum order size;
   - are **not banned** (see section 6).
3. Take the 40 with the biggest reward pools. For each, read the live order book and estimate:
   - **expected rewards per day** = pool × estimated share (section 1);
   - **expected swing loss per day** from a week of hourly prices: every hourly move bigger than the
     bot's quote distance is counted as `(move − distance) × order size`;
   - **score = expected rewards − 50% of expected swing loss** (version D used 100% and was too timid).
4. Drop markets with a score ≤ 0. Rank by score per dollar needed and take the best **2** that fit in
   the **$50** budget.

---

## 3. Every second, for each chosen market

The bot keeps a live copy of each order book and the latest trades through Polymarket's websocket
(it re-reads the book over the web if nothing arrives for 30 seconds).

**Step 1: Did an order fill?** A waiting buy order counts as filled if the best sell price, the
midpoint, or any trade reaches its price (and the reverse for sell orders). This is deliberately
pessimistic: it counts the costly fills (price moving through), but not harmless ones.

**Step 2: Collect rewards** for the time the orders were waiting, using the scoring in section 1.

**Step 3: Time limit (new in E).** If the bot has held shares in this market for **3 hours**, it sells
them immediately at the best available price, paying the taker fee (`shares × rate × p × (1 − p)`,
with the market's own rate). This stops one bad position from sitting for days.

**Step 4: Safety checks.** Orders are pulled, and the market paused, when:

| Trigger | Pause |
|---|---|
| Price moved **2¢ or more within 60 s** | 10 min |
| One trade of **$100+**, or **5× the minimum size** | 5 min |
| **3 trades in the same direction within 10 s** | 5 min |
| Bot's reward share above **30%** (others pulled out, usually before news) | 10 min |
| Losses for the UTC day reach **$7.50** (15%) | rest of the day |

**Step 5: Place orders again** (unless paused):
- **Where:** centred on the **microprice** (the midpoint nudged toward the side with less waiting
  volume, where the next move is more likely to go), **halfway** to the edge of the reward zone.
- **Jumpy markets (new in E):** if the price ranged **1¢ or more in the last 10 minutes**, orders go
  **¾ of the way** out instead of halfway.
- **Queue cover (new in E):** a new order is only placed at a price where other traders already have
  at least **2×** the bot's size waiting at the same or a better price, so small trades hit them first.
  If no such price exists in the reward zone, the order goes to the far edge.
- **Size:** the market's minimum reward size (usually 20 shares, about $4–$19).
- **Holding shares:** only the selling side is quoted, at **half** the usual distance, to exit faster.
- **Max stuck (new in E):** if **2** markets already have stuck shares, no new orders are placed in
  any other market until one is cleared.
- **Money check:** an order is only placed if enough of the $50 is free after other waiting orders.

---

## 4. Money accounting (paper)

- Cash starts at **$50**. Buying YES at `p` costs `p × size`; a sell order is modelled as buying NO at
  `1 − p` (that is how Polymarket works without owning shares first).
- Holding both YES and NO of the same market is merged back into **$1** per pair.
- Shares still held are valued at the current midpoint.
- **Net result = cash + value of held shares + rewards − $50.**
- Resting (maker) orders pay **no fee**. Only the time-limit sale pays the taker fee.

---

## 5. Settings for $50

| Setting | Value |
|---|---|
| Budget | $50 |
| Markets at once | 2 |
| Most per market | $25 |
| Daily loss limit | $7.50 |
| Quote distance | 50% of max spread (75% when jumpy, 25% when selling stuck shares) |
| Time limit on stuck shares | 3 hours |
| Stuck markets at once | 2 |
| Queue cover | 2× own size |
| Ban after losing | $0.30 net, for 24 h |
| Re-pick markets | every 60 min |

---

## 6. Learning

At each hourly re-pick the bot adds up, per market: rewards earned + cash in/out from fills + value of
anything still held. A market that has lost more than **$0.30** is **banned for 24 hours**. Bans are
stored in the database, so they survive restarts.

---

## 7. Known weak spots (where to look for improvements)

**What the paper test cannot know:**
1. **Queue position:** real orders wait in line behind earlier ones at the same price. The paper bot
   assumes it is filled as soon as the price reaches it, and never filled otherwise.
2. **Real reward share:** estimated from total volume per price, not per trader. Polymarket's own
   rewards page or API would show the actual payout once live.
3. **Reaction time:** paper reacts within about a second; real cancels take a few hundred milliseconds
   more, and fast traders are quicker.
4. **The bot's own effect:** real orders change what others do. Paper orders are invisible.

**Strategy gaps worth testing next:**
5. **Scheduled events:** a calendar of known announcement times (elections, product launches,
   earnings, court dates) to stop quoting a few hours before each.
6. **News watching:** stop quoting a market when related headlines appear (an AI could read them).
   Costs money per check, and fast traders still react first.
7. **Smarter exit price:** sell stuck shares just above what was paid when possible, instead of only
   "half distance, then time limit".
8. **Related markets:** many markets move together (e.g. the different NVIDIA CEO candidates). Holding
   stuck shares in two related markets is really one bigger bet; treat them as one.
9. **Maker rebates:** some markets also pay makers part of the taker fees. The bot ignores this income.
10. **Size:** always the minimum size. More money would allow more markets rather than bigger orders.
11. **Time of day:** news-driven moves cluster around US daytime. Quoting further out, or not at all,
    during those hours could reduce stuck shares.
12. **Per-market settings:** learn the best quote distance per market from its own fills, instead of one
    distance for all.

**Before real money:** a week of paper results on an always-on machine, including at least one
news-driven drop, and a live check that the server's location is not blocked
(`curl https://polymarket.com/api/geoblock`).
