# Findings: where are Polymarket's prices wrong? (12 Nov 2026)

Data: resolved yes/no markets with $5k+ trading volume from the last 12 months. Prices are hourly
prices no more than 2 hours old at each point in time, taken only while the market was still open.
Full tables: `CALIBRATION.md` (fair sample) and `CALIBRATION_by_volume.md` (biased sample, kept as a warning).
Script: `calibration.py`.

## 1. Choosing markets by volume gives false answers
The first run used the 3,000 highest-volume markets. Surprising outcomes attract heavy trading, so that
sample over-represents long shots that came in, and long shots looked underpriced by 7–14 points.
With a fair sample (5,671 markets chosen by end date, so outcomes can't affect selection) the effect
disappears. Any future study must not choose markets by total volume.

## 2. Long shots and favourites: no tradable edge
In the fair sample, markets priced 95–98¢ resolved YES 99.3–99.7% of the time (about +2.5–3.2¢ per
share at every time point). But as an actual strategy (buy each market's favourite side the first
time it reaches 93–98¢, hold to the end; 1,634 trades from 862 events) the result was
**+0.03¢ ± 1.2¢ per share: no edge**. 93–95¢ favourites lost money. The table overstated it because
it counted each market up to five times and only looked at the YES side.

## 3. Fading sudden rises: a strong signal in the data, not yet proven tradable
After a 12-hour rise of 10¢+ (at least 3 days before the end), buying NO and selling 2 days later:

| | Trades | Move back in 2 days | Held to end | Profitable after ~2¢ costs |
|---|---|---|---|---|
| All | 1,727 (969 events, ~33/week) | +9.7¢ (median +4.5¢) | +11.3¢ | 55% |
| Price after rise 30–60¢ | 837 | **+15.7¢** | **+24.9¢** | **72%** |
| Price after rise 60–97¢ | 673 | +2.1¢ | −4.3¢ | 28% |
| Markets with $1M+ volume | 30 | +3.3¢ (not meaningful) | | 43% |

Strongest in small and mid-sized markets. **Caution:** historical prices are only available every
12 hours and without order books. In thin markets a "rise" can be one odd trade with nobody willing to
trade at that price, so part of the reversal may not be capturable. Only a live paper test that checks
the real order book at the moment of the rise can tell.

## Next step
Version G (paper): watch markets live; when a market rises 15¢+ to between 30¢ and 60¢, record the
price at which NO could actually be bought from the live order book, then follow the position for
2 days (or to a target / stop). Judge it on real fills after costs over 1–2 weeks (~30+ trades/week
expected) before any real money.
