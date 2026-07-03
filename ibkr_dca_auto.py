#!/usr/bin/env python3
"""
IBKR DCA Auto-Trader  (production)
==================================
Fully automated monthly dollar-cost-averaging into a fixed ETF portfolio,
executed through IB Gateway / TWS.

Portfolio (all EUR-denominated, Xetra/IBIS2 — works with EU IBKR accounts):
  40%  SXR8   iShares Core S&P 500 UCITS ETF             (Xetra, EUR)
  30%  XNAS   Xtrackers Nasdaq-100 UCITS ETF             (Xetra, EUR)
  20%  EGLN   iShares Physical Gold ETC                  (Xetra, EUR)
   5%  XDEQ   Xtrackers MSCI World Quality Factor UCITS  (Xetra, EUR)
   5%  IWMO   iShares MSCI World Momentum Factor UCITS   (Xetra, EUR)

DESIGN PRINCIPLES (this is real money on autopilot — safety first):
  • Idempotent per leg, per month. The script can be run many times in a
    month (cron fires on several days as a safety net) but each ETF is bought
    exactly ONCE per calendar month. State is kept in dca_state.json.
  • Never trades on margin. It reads the account's available cash and never
    places orders whose total cost exceeds it.
  • Only trades when the exchange is genuinely open (parses IBKR trading
    hours, so holidays/weekends are handled correctly). If closed, it exits
    cleanly WITHOUT marking the month done, so the next scheduled run retries.
  • Sweeps available cash (capped) into the buys, so nothing sits idle, but a
    hard ceiling blocks runaway orders from a data error or double deposit.
  • Sanity-checks every price against last month; a wild move (bad ticker,
    split, data glitch) skips that leg instead of mis-sizing an order.
  • Logs every action to dca_trade_log.csv and posts a macOS notification on
    completion or failure.

USAGE:
  python ibkr_dca_auto.py            # normal run (places live orders if open)
  python ibkr_dca_auto.py --dry-run  # compute & print orders, place NOTHING
  python ibkr_dca_auto.py --force     # ignore the once-per-month guard

STRATEGY START: deferred to START_DATE (September 2027). Before that date the
script trades nothing; the cron below can stay installed harmlessly, or be
added closer to the start. Fire on the first SEVEN days of the month so a
weekend/holiday 1st still gets caught (it trades on the first open day); the
once-per-month guard ensures it still buys only once:
  30 10 1-7 * * /path/to/python3 /path/to/ibkr_dca_auto.py >> /path/to/dca_cron.log 2>&1

WARNING: With PAPER_TRADE = False this places REAL orders with REAL money.
"""

import os
import sys
import math
import json
import logging
import datetime
import csv
import argparse
import subprocess
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_insync import IB, Contract, MarketOrder

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Set your IBKR account id via environment variable, e.g. export IBKR_ACCOUNT_ID="U1234567"
ACCOUNT_ID   = os.environ.get("IBKR_ACCOUNT_ID", "YOUR_ACCOUNT_ID")
PORT         = 7496           # 7496 = live Gateway/TWS, 7497 = paper
HOST         = "127.0.0.1"
CLIENT_ID    = 1
PAPER_TRADE  = False          # True = simulate only, place no real orders

# Do not begin investing until this date. The strategy is intentionally
# deferred to September 2027; any run before this date trades nothing.
START_DATE   = datetime.date(2027, 9, 1)

# Money management ----------------------------------------------------------
TARGET_MONTHLY_GROSS = 1000.0  # nominal € you intend to invest each month
MAX_MONTHLY_GROSS    = 1300.0  # hard ceiling: never spend more than this in a
                               #   single month (blocks runaway from a data
                               #   error or an accidental double deposit)
MIN_FUNDS_FRACTION   = 0.90    # if available cash < this × TARGET, assume the
                               #   monthly deposit hasn't settled yet; skip and
                               #   retry on the next scheduled run
CASH_BUFFER          = 5.0     # € left untouched to absorb fee/FX rounding
TX_FEE_PER_LEG       = 3.0     # IBKR min commission per order (€)
QTY_DECIMALS         = 4       # fractional-share precision (IBKR sizeIncrement)
MAX_PRICE_JUMP       = 0.40    # reject a price that moved >40% vs last month

# Portfolio weights (must sum to 1.0). All EUR on Xetra (IBIS2).
PORTFOLIO = {
    "SXR8":  0.40,   # iShares Core S&P 500 UCITS ETF
    "XNAS":  0.30,   # Xtrackers Nasdaq-100 UCITS ETF
    "EGLN":  0.20,   # iShares Physical Gold ETC
    "XDEQ":  0.05,   # Xtrackers MSCI World Quality Factor UCITS
    "IWMO":  0.05,   # iShares MSCI World Momentum Factor UCITS
}

# conIds confirmed via IBKR API (bypass symbol-resolution ambiguity).
CONIDS = {
    "SXR8":  75776072,
    "XNAS":  468775632,
    "EGLN":  257200855,
    "XDEQ":  167234536,
    "IWMO":  183908189,
}

# Yahoo Finance tickers for price lookup (no API key / market-data sub needed).
YAHOO_TICKERS = {
    "SXR8":  "SXR8.DE",
    "XNAS":  "XNAS.DE",
    "EGLN":  "PPFB.DE",   # Yahoo lists EGLN under its Xetra local symbol PPFB
    "XDEQ":  "XDEQ.DE",
    "IWMO":  "IS3R.DE",   # Yahoo lists IWMO under its Xetra local symbol IS3R
}

# Representative contract used to read exchange trading hours.
HOURS_PROBE_CONID = CONIDS["SXR8"]
EXCHANGE          = "IBIS2"

BASE_DIR   = Path(__file__).parent
LOG_FILE   = BASE_DIR / "dca_trade_log.csv"
STATE_FILE = BASE_DIR / "dca_state.json"

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dca_trader")


def notify(title: str, message: str) -> None:
    """Best-effort macOS desktop notification. Never raises."""
    try:
        safe = message.replace('"', "'")
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe}" with title "{title}"'],
            timeout=5, check=False,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# STATE (persisted across runs — the basis for once-per-month idempotency)
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"State file unreadable ({e}); starting fresh.")
    return {"last_prices": {}, "months": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)   # atomic on POSIX


def leg_done(state: dict, month: str, ticker: str) -> bool:
    """A leg counts as done once an order is live at the broker (filled OR
    submitted) — so we never place a second order for it the same month."""
    rec = state.get("months", {}).get(month, {}).get(ticker)
    return bool(rec) and rec.get("status") in ("FILLED", "SUBMITTED")


def record_leg(state: dict, month: str, ticker: str, rec: dict) -> None:
    state.setdefault("months", {}).setdefault(month, {})[ticker] = rec


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS (authoritative — parsed from IBKR, handles holidays)
# ══════════════════════════════════════════════════════════════════════════════

def market_is_open(ib: IB) -> bool:
    """Return True iff the exchange is in a continuous-trading session right now,
    according to IBKR's own published trading hours (so holidays count)."""
    try:
        c = Contract(conId=HOURS_PROBE_CONID, exchange=EXCHANGE)
        cds = ib.reqContractDetails(c)
        if not cds:
            log.warning("Could not read trading hours; falling back to weekday check.")
            return datetime.date.today().weekday() < 5
        cd = cds[0]
        tz = ZoneInfo("Europe/Berlin")          # MET/CET, with DST
        now = datetime.datetime.now(tz)
        today_key = now.strftime("%Y%m%d")
        for seg in cd.liquidHours.split(";"):
            if not seg or not seg.startswith(today_key):
                continue
            if "CLOSED" in seg:
                log.info(f"Exchange CLOSED today ({today_key}).")
                return False
            # format: YYYYMMDD:HHMM-YYYYMMDD:HHMM
            try:
                open_part, close_part = seg.split("-")
                o = datetime.datetime.strptime(open_part, "%Y%m%d:%H%M").replace(tzinfo=tz)
                c_ = datetime.datetime.strptime(close_part, "%Y%m%d:%H%M").replace(tzinfo=tz)
                if o <= now <= c_:
                    return True
                log.info(f"Outside session window {o:%H:%M}-{c_:%H:%M} "
                         f"(now {now:%H:%M} {now.tzname()}).")
                return False
            except Exception:
                continue
        # No segment matched today — be conservative.
        log.warning("No trading-hours segment matched today; treating as closed.")
        return False
    except Exception as e:
        log.warning(f"market_is_open check failed ({e}); falling back to weekday check.")
        return datetime.date.today().weekday() < 5


# ══════════════════════════════════════════════════════════════════════════════
# PRICES (Yahoo Finance — no subscription needed) + sanity check
# ══════════════════════════════════════════════════════════════════════════════

def get_price_yahoo(ticker: str) -> float | None:
    yahoo_sym = YAHOO_TICKERS[ticker]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
           f"?interval=1d&range=5d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        valid = [c for c in closes if c is not None and c > 0]
        if valid:
            return float(valid[-1])
    except Exception as e:
        log.error(f"  {ticker}: Yahoo price fetch failed: {e}")
    return None


def sane_price(ticker: str, price: float, state: dict) -> bool:
    if price is None or price <= 0 or price > 100_000:
        log.error(f"  {ticker}: price {price} is implausible — SKIPPING leg.")
        return False
    last = state.get("last_prices", {}).get(ticker)
    if last:
        move = abs(price - last) / last
        if move > MAX_PRICE_JUMP:
            log.error(f"  {ticker}: price moved {move:.0%} vs last (€{last:.2f}"
                      f"→€{price:.2f}) — looks wrong, SKIPPING leg.")
            return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# FUNDS
# ══════════════════════════════════════════════════════════════════════════════

def available_eur(ib: IB) -> float:
    """Cash available to trade WITHOUT borrowing (EUR)."""
    vals = {(v.tag, v.currency): v.value
            for v in ib.accountValues() if v.account == ACCOUNT_ID}
    for tag in ("AvailableFunds", "TotalCashValue"):
        if (tag, "EUR") in vals:
            try:
                return float(vals[(tag, "EUR")])
            except ValueError:
                pass
    return 0.0


# ══════════════════════════════════════════════════════════════════════════════
# ORDER COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def compute_budget(available: float) -> float | None:
    """Decide this month's gross spend. Returns None if funds are insufficient
    (deposit likely pending) — caller should skip and retry later."""
    if available < TARGET_MONTHLY_GROSS * MIN_FUNDS_FRACTION:
        log.warning(f"Available €{available:.2f} < "
                    f"{MIN_FUNDS_FRACTION:.0%} of target "
                    f"€{TARGET_MONTHLY_GROSS:.0f}. Deposit may be pending — "
                    f"will retry on the next scheduled run.")
        return None
    gross = min(available - CASH_BUFFER, MAX_MONTHLY_GROSS)
    return max(gross, 0.0)


def calculate_orders(ib: IB, state: dict, month: str, gross: float) -> list[dict]:
    """Build the list of legs to trade this run (skipping any already done)."""
    n_legs = len(PORTFOLIO)
    total_fees = n_legs * TX_FEE_PER_LEG
    investable_shares = gross - total_fees
    if investable_shares <= 0:
        log.error(f"Gross €{gross:.2f} cannot cover €{total_fees:.2f} in fees.")
        return []

    orders = []
    for ticker, weight in PORTFOLIO.items():
        if leg_done(state, month, ticker):
            log.info(f"  {ticker}: already done for {month} — skipping.")
            continue

        leg_budget = investable_shares * weight

        contract = Contract(conId=CONIDS[ticker], exchange=EXCHANGE)
        try:
            q = ib.qualifyContracts(contract)
            if not q:
                log.error(f"  {ticker}: qualify failed — SKIPPING leg.")
                continue
            contract = q[0]
        except Exception as e:
            log.error(f"  {ticker}: qualify error {e} — SKIPPING leg.")
            continue

        price = get_price_yahoo(ticker)
        if not sane_price(ticker, price, state):
            continue
        state.setdefault("last_prices", {})[ticker] = price

        qty = math.floor((leg_budget / price) * 10**QTY_DECIMALS) / 10**QTY_DECIMALS
        if qty < 10**-QTY_DECIMALS:
            log.warning(f"  {ticker}: budget €{leg_budget:.2f} too small at "
                        f"€{price:.2f} — SKIPPING leg.")
            continue

        orders.append({
            "ticker": ticker, "contract": contract, "qty": qty,
            "price": price, "leg_budget": leg_budget,
            "cost_est": qty * price,
        })
        log.info(f"  {ticker}: price €{price:.4f}  budget €{leg_budget:.2f}  qty {qty}")

    return orders


# ══════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def write_log(row: dict) -> None:
    fields = ["date", "ticker", "action", "qty", "ref_price",
              "fill_price", "fill_qty", "eur_invested", "status", "note"]
    exists = LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def place_and_fill(ib: IB, orders: list[dict], state: dict, month: str,
                   dry_run: bool) -> list[dict]:
    today = datetime.date.today().isoformat()
    results = []

    for o in orders:
        tk, contract, qty, price = o["ticker"], o["contract"], o["qty"], o["price"]

        if dry_run or PAPER_TRADE:
            tag = "DRY-RUN" if dry_run else "PAPER"
            log.info(f"  [{tag}] would BUY {qty} {tk} (~€{o['cost_est']:.2f})")
            results.append({**o, "status": tag, "fill_price": price, "fill_qty": qty})
            continue

        trade = ib.placeOrder(contract, MarketOrder("BUY", qty, account=ACCOUNT_ID))
        log.info(f"  {tk}: order {trade.order.orderId} submitted (BUY {qty})")

        # Poll up to 60s for a terminal/working status.
        status = "UNKNOWN"
        for _ in range(12):
            ib.sleep(5)
            s = trade.orderStatus.status
            if s == "Filled":
                status = "FILLED"; break
            if s in ("Cancelled", "ApiCancelled", "Inactive"):
                status = "REJECTED"; break
            if s in ("Submitted", "PreSubmitted") and trade.orderStatus.filled > 0:
                status = "FILLED"; break
            status = "SUBMITTED" if s in ("Submitted", "PreSubmitted") else s

        fp = trade.orderStatus.avgFillPrice or price
        fq = trade.orderStatus.filled or 0
        note = ""
        if status == "REJECTED":
            logs = [le.message for le in trade.log] if trade.log else []
            note = "; ".join(logs[-2:])
            log.error(f"  {tk}: REJECTED — {note}")
        else:
            log.info(f"  {tk}: {status}  filled {fq}/{qty}  avg €{fp:.4f}")

        write_log({"date": today, "ticker": tk, "action": "BUY", "qty": qty,
                   "ref_price": price, "fill_price": fp, "fill_qty": fq,
                   "eur_invested": o["leg_budget"], "status": status, "note": note})

        # Record in state only if the order actually reached the broker.
        if status in ("FILLED", "SUBMITTED"):
            record_leg(state, month, tk, {
                "status": status, "qty": qty, "fill_price": fp,
                "fill_qty": fq, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            })
            save_state(state)

        results.append({**o, "status": status, "fill_price": fp, "fill_qty": fq})

    return results


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="IBKR monthly DCA auto-trader")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print orders but place nothing")
    ap.add_argument("--force", action="store_true",
                    help="ignore the once-per-month guard")
    args = ap.parse_args()

    today = datetime.date.today()
    month = today.strftime("%Y-%m")

    log.info("=" * 72)
    log.info(f"IBKR DCA Auto-Trader  |  {today}  |  month {month}  |  "
             f"{'DRY-RUN' if args.dry_run else ('PAPER' if PAPER_TRADE else 'LIVE')}")
    log.info("=" * 72)

    # Hard start-date gate. The strategy is deferred to START_DATE; before then
    # the script trades NOTHING, even if a scheduled job fires by mistake.
    # (--dry-run still previews so you can sanity-check the setup any time.)
    if today < START_DATE and not args.dry_run:
        log.info(f"Strategy starts {START_DATE.isoformat()}; today is {today}. "
                 f"Nothing to do for {(START_DATE - today).days} more day(s).")
        sys.exit(0)

    if abs(sum(PORTFOLIO.values()) - 1.0) > 1e-6:
        log.error(f"Weights sum to {sum(PORTFOLIO.values())}, must be 1.0. Abort.")
        sys.exit(1)

    state = load_state()

    # Once-per-month guard (per leg). If every leg is already done, stop early.
    if not args.force and all(leg_done(state, month, tk) for tk in PORTFOLIO):
        log.info(f"All legs already executed for {month}. Nothing to do.")
        sys.exit(0)

    ib = IB()
    log.info(f"Connecting to IB at {HOST}:{PORT} (clientId={CLIENT_ID})...")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20)
    except Exception as e:
        log.error(f"Connection failed: {e}. Is IB Gateway running with API on?")
        notify("DCA Trader — FAILED", "Could not connect to IB Gateway.")
        sys.exit(1)
    log.info(f"Connected. Account {ACCOUNT_ID}.")

    try:
        # 1) Market must be genuinely open (unless previewing).
        if not args.dry_run and not market_is_open(ib):
            log.info("Market not open now — exiting without trading; "
                     "the next scheduled run will retry.")
            sys.exit(0)

        # 2) Funds check (never trade on margin; detect pending deposit).
        avail = available_eur(ib)
        log.info(f"Available cash: €{avail:.2f}")
        gross = compute_budget(avail)
        if gross is None and not args.dry_run:
            notify("DCA Trader — waiting",
                   f"Available €{avail:.0f} < target. Will retry next run.")
            sys.exit(0)
        if gross is None:                      # dry-run with low funds: preview anyway
            gross = min(max(avail - CASH_BUFFER, 0.0), MAX_MONTHLY_GROSS)
        log.info(f"This month's gross spend: €{gross:.2f} "
                 f"(target €{TARGET_MONTHLY_GROSS:.0f}, ceiling €{MAX_MONTHLY_GROSS:.0f})")

        # 3) Build orders.
        orders = calculate_orders(ib, state, month, gross)
        if not orders:
            log.error("No valid orders computed.")
            notify("DCA Trader — nothing traded", "No valid orders this run.")
            sys.exit(1)

        # 4) Hard guard: total must not exceed available cash.
        total_cost = sum(o["cost_est"] for o in orders) + len(orders) * TX_FEE_PER_LEG
        log.info("─" * 56)
        log.info(f"Planned spend €{total_cost:.2f} of €{avail:.2f} available:")
        for o in orders:
            log.info(f"  BUY {o['qty']:>9.4f} × {o['ticker']:<6} "
                     f"@ €{o['price']:.4f}  ≈ €{o['cost_est']:.2f}")
        log.info("─" * 56)
        if total_cost > avail and not (args.dry_run or PAPER_TRADE):
            log.error(f"Total €{total_cost:.2f} exceeds available €{avail:.2f}. "
                      f"Refusing to trade on margin. Abort.")
            notify("DCA Trader — ABORTED", "Order total exceeds cash. No trade.")
            sys.exit(1)

        # 5) Execute.
        results = place_and_fill(ib, orders, state, month, args.dry_run)

        # 6) Summary + notification.
        ok   = [r for r in results if r["status"] in ("FILLED", "SUBMITTED", "PAPER", "DRY-RUN")]
        bad  = [r for r in results if r["status"] not in ("FILLED", "SUBMITTED", "PAPER", "DRY-RUN")]
        spent = sum(r["fill_qty"] * r["fill_price"] for r in ok)
        log.info("=" * 72)
        log.info("TRADE SUMMARY:")
        for r in results:
            log.info(f"  {r['ticker']:<6} {r['status']:<9} "
                     f"qty {r['fill_qty']}/{r['qty']}  @ €{r['fill_price']:.4f}")
        log.info(f"Filled/placed: {len(ok)}/{len(results)}  |  "
                 f"≈ €{spent:.2f} deployed")
        log.info(f"Logs: {LOG_FILE.name}, {STATE_FILE.name}")
        log.info("=" * 72)

        if args.dry_run:
            notify("DCA Trader — dry run", f"{len(ok)} legs previewed.")
        elif bad:
            notify("DCA Trader — PARTIAL",
                   f"{len(bad)} leg(s) failed; {len(ok)} ok. Check log.")
            sys.exit(2)
        else:
            notify("DCA Trader — done",
                   f"{len(ok)} legs, ≈€{spent:.0f} invested for {month}.")

    finally:
        ib.disconnect()
        log.info("Disconnected from IBKR.")


if __name__ == "__main__":
    main()
