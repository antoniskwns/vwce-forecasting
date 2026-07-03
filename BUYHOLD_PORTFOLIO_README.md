# Buy-and-Hold Portfolio — Operating Guide

A 10-year buy-and-hold strategy run by `ibkr_buyhold_portfolio.py`. It runs in the **same
IBKR account** as the monthly DCA strategy but is kept strictly separate from it. The
account id is read from the `IBKR_ACCOUNT_ID` environment variable.

## The portfolio (60 / 25 / 15, all EUR on Xetra)

| Weight | Ticker | Fund | Notes |
|--------|--------|------|-------|
| 60% | SXR8 | iShares Core S&P 500 UCITS ETF (Acc) | Core equity |
| 25% | XNAS | Xtrackers Nasdaq-100 UCITS ETF (Acc) | Growth/tech |
| 15% | EGLN | iShares Physical Gold ETC | Diversifier / crash hedge |

**Gold leg naming:** the IBKR app shows the gold ETC as **"EGLN LSEETF"** with local symbol
**PPFB**. That is the same fund (conId 257200855). The order routes it to **Xetra in EUR**;
the London display name is cosmetic and does not change where the trade executes.

## Lifecycle

| When | Action | How |
|------|--------|-----|
| First €5k available | **Tranche 1**: invest €5,000 into 60/25/15 | Run manually (below), during Xetra hours |
| January 2027 | **Tranche 2**: invest €5,000 into 60/25/15 | Automatic via cron |
| Every January after | **Rebalance** the slice back to 60/25/15 | Automatic via cron |

After both tranches (~€10k) no new money ever enters this strategy. Rebalancing is
**cash-neutral** (sells fund buys), so it never touches the cash deposited for the separate
monthly-DCA strategy.

## How the two strategies stay separate

This account holds two independent strategies. This one keeps clear of the other by:

- Separate files: `buyhold_state.json`, `buyhold_trade_log.csv`, `buyhold_cron.log`.
- Separate API client id (`CLIENT_ID = 2`; the DCA trader uses 1) — both can run at once.
- Tracking **only the shares it bought for this strategy** in `buyhold_state.json` →
  `holdings`. It never reads account-level positions, so it can never rebalance the DCA
  strategy's holdings.

## Running it

```bash
# Preview anytime (bypasses market-hours + funds guards, places NOTHING):
python3 ibkr_buyhold_portfolio.py --dry-run

# Real run (tranche 1): during Xetra hours 09:00–17:45 CET, Mon–Fri:
python3 ibkr_buyhold_portfolio.py
```

The script decides what to do from the date + state. It is safe to run anytime:
- Before the cash is in → the **funds guard** refuses (won't trade on margin).
- Outside market hours → exits cleanly, no orders.
- Already-done event → skips (once-per-event guard).

## The cron

```text
30 10 1-7 1 * /path/to/python3 /path/to/ibkr_buyhold_portfolio.py >> /path/to/buyhold_cron.log 2>&1
```

10:30, days 1–7, **January only**. Handles tranche 2 (Jan 2027) and every annual rebalance
after. The 1–7 window catches a holiday/weekend Jan 1; the once-per-event guard ensures it
still acts only once. Check it with `crontab -l`.

## IB Gateway must be up when the cron fires

The script can only trade if **IB Gateway is running and logged in** at the moment the cron
fires (each January) and the machine is awake. IBKR forces a daily logout, so a calendar
reminder for early January to log into the Gateway is worthwhile. If it can't connect, the
run fails cleanly (check `buyhold_cron.log`) and retries on the next day in the 1–7 window.

## Checking on it

- **Holdings:** open `buyhold_state.json` → `holdings` (shares per ticker) and `events`.
- **Trade history:** `buyhold_trade_log.csv`.
- **Cron output:** `buyhold_cron.log`.

## Live/paper switch

`PAPER_TRADE` near the top of the script:

- `False` = LIVE, real orders.
- `True`  = simulate: reads real data, prints "would BUY/SELL", places nothing.
