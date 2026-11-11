# Polymarket scanner (read-only)

Measures whether Polymarket offers opportunities a bot could use, before any money is risked. No account, no orders.

It logs, every couple of minutes:
1. **Multi-outcome arbitrage** in events where exactly one outcome wins: buying YES on every outcome for less than $1 (or NO on every outcome for less than N-1), after each market's live taker fee, re-checked on the live order books with the size actually available.
2. **Liquidity rewards**: markets that pay a daily amount to people who keep orders near the price.
3. **Wide spreads** on busy markets.

```bash
pip install requests
python scanner.py once          # one scan
python scanner.py scan --minutes 480
python scanner.py report --out report.md
```

Fee formula (Polymarket docs, 2026): taker fee per share = rate × p × (1 − p), with the rate read from each market. Makers pay no fee.
