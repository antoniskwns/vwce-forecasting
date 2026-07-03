#!/usr/bin/env python3
"""
IBKR Buy-and-Hold Portfolio Trader  (production)
================================================
Two-phase strategy for a 10-year buy-and-hold portfolio, executed through
IB Gateway / TWS. It runs in the SAME account as the monthly DCA trader but is
kept strictly separate from it (see "ACCOUNT SHARING" below).

Target portfolio (all EUR-denominated, Xetra/IBIS2):
  60%  SXR8   iShares Core S&P 500 UCITS ETF   (Xetra, EUR)
  25%  XNAS   Xtrackers Nasdaq-100 UCITS ETF   (Xetra, EUR)
  15%  EGLN   iShares Physical Gold ETC        (Xetra, EUR)

LIFECYCLE
  Phase 1  INVEST   — two lump-sum tranches into the 60/25/15 split:
                        • €5,000 now (first run)
                        • €5,000 in January 2027 (second run)
                      After both tranches, NO new money ever enters this
                      strategy again.
  Phase 2  REBALANCE — once a year (each January) the script restores the
                       60/25/15 weights on this strategy's slice only, by computing the
                       drift and placing the net buy/sell trades. Rebalancing
                       is internally cash-neutral (sells fund buys).

ACCOUNT SHARING (why this never touches the other strategy)
  This account also holds a separate monthly-DCA strategy's positions. To stay
  separated, this script does NOT look at account-level positions. It tracks
  exactly the shares it bought for this strategy — "this strategy's slice" — in
  its OWN state file (buyhold_state.json), and rebalances only that slice. The
  account total is irrelevant to its logic. Cash deposited for the OTHER
  strategy is never spent here: Phase-1 buys are capped at the per-tranche
  amount, and Phase-2 rebalancing only ever trades within its tracked holdings.

DESIGN PRINCIPLES (real money on autopilot — safety first; same as DCA trader)
  • Idempotent per phase/event. A tranche is invested once; a year's rebalance
    happens once. Re-runs are safe.
  • Never trades on margin. Phase-1 buys never exceed available cash.
  • Only trades when the exchange is genuinely open (parses IBKR hours).
  • Price sanity checks; fractional shares; full logging; macOS notifications.

USAGE
  python ibkr_buyhold_portfolio.py            # do whatever this run's phase requires
  python ibkr_buyhold_portfolio.py --dry-run  # compute & print, place NOTHING
  python ibkr_buyhold_portfolio.py --force    # ignore the once-per-event guard

SCHEDULING (macOS cron — run `crontab -e`)
  Tranche 1 (now): run manually once, today, during market hours.
  Tranche 2 + yearly rebalance: fire on the first SEVEN days of EACH JANUARY
  (the once-per-event guard ensures one action only):
    30 10 1-7 1 * /path/to/python3 /path/to/ibkr_buyhold_portfolio.py >> /path/to/buyhold_cron.log 2>&1

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
CLIENT_ID    = 2              # DIFFERENT from the DCA trader (1) so both can run
PAPER_TRADE  = False          # False = LIVE: places REAL orders with REAL money

# Target weights (must sum to 1.0). All EUR, all routed to Xetra (IBIS2) —
# verified 2026-06-19: each conId below settles in EUR and routes to IBIS2.
# NOTE on the gold leg: its conId (257200855) is iShares Physical Gold ETC.
# Its PRIMARY listing is London (LSEETF, ticker "EGLN"), so the IBKR app may
# DISPLAY it as "EGLN LSEETF" with local symbol "PPFB". That is cosmetic — our
# order is pinned by conId + exchange="IBIS2", so it executes on Xetra in EUR,
# exactly like the other two legs. (Yahoo price symbol is PPFB.DE.)
PORTFOLIO = {
    "SXR8":  0.60,   # iShares Core S&P 500 UCITS ETF        (EUR, Xetra)
    "XNAS":  0.25,   # Xtrackers Nasdaq-100 UCITS ETF         (EUR, Xetra)
    "EGLN":  0.15,   # iShares Physical Gold ETC (app: EGLN/PPFB) (EUR, Xetra)
}

# Investment tranches: event-key -> (€ amount, earliest date it may run).
# Each tranche is invested exactly once. The first is available immediately;
# the second only on/after 2027-01-01.
TRANCHES = {
    "tranche_1": {"amount": 5000.0, "not_before": datetime.date(2026, 6, 19)},
    "tranche_2": {"amount": 5000.0, "not_before": datetime.date(2027, 1, 1)},
}

# Annual rebalance runs only in this month, once per year, after both tranches.
REBALANCE_MONTH       = 1        # January
REBALANCE_MIN_DRIFT   = 0.02     # skip trades smaller than 2% of slice (avoid
                                 #   churning tiny amounts / wasting fees)

# Safety / mechanics --------------------------------------------------------
CASH_BUFFER     = 5.0     # € left untouched on a tranche buy (fee/rounding)
TX_FEE_PER_LEG  = 3.0     # IBKR min commission per order (€)
QTY_DECIMALS    = 4       # fractional-share precision (IBKR sizeIncrement)
MAX_PRICE_JUMP  = 0.60    # reject a price that moved >60% vs last seen
                          #   (wider than the monthly DCA's 40%, since events
                          #    here can be a YEAR apart)

# conIds confirmed via IBKR API (bypass symbol-resolution ambiguity).
CONIDS = {
    "SXR8":  75776072,
    "XNAS":  468775632,
    "EGLN":  257200855,
}

# Yahoo Finance tickers for price lookup (no API key / market-data sub needed).
YAHOO_TICKERS = {
    "SXR8":  "SXR8.DE",
    "XNAS":  "XNAS.DE",
    "EGLN":  "PPFB.DE",   # Yahoo lists EGLN under its Xetra local symbol PPFB
}

HOURS_PROBE_CONID = CONIDS["SXR8"]
EXCHANGE          = "IBIS2"

BASE_DIR   = Path(__file__).parent
LOG_FILE   = BASE_DIR / "buyhold_trade_log.csv"
STATE_FILE = BASE_DIR / "buyhold_state.json"

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("buyhold_trader")


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
# STATE
#   {
#     "last_prices": {ticker: float},
#     "holdings":    {ticker: shares},   # this strategy's slice only (cumulative)
#     "events":      {event_key: {...}}  # once-per-event idempotency markers
#   }
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
        except Exception as e:
            log.warning(f"State file unreadable ({e}); starting fresh.")
            s = {}
    else:
        s = {}
    s.setdefault("last_prices", {})
    s.setdefault("holdings", {tk: 0.0 for tk in PORTFOLIO})
    s.setdefault("events", {})
    for tk in PORTFOLIO:
        s["holdings"].setdefault(tk, 0.0)
    return s


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)   # atomic on POSIX


def event_done(state: dict, key: str) -> bool:
    return bool(state.get("events", {}).get(key, {}).get("done"))


def mark_event(state: dict, key: str, info: dict) -> None:
    rec = {"done": True, "ts": datetime.datetime.now().isoformat(timespec="seconds")}
    rec.update(info)
    state.setdefault("events", {})[key] = rec
    save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
# MARKET HOURS (authoritative — parsed from IBKR, handles holidays)
# ══════════════════════════════════════════════════════════════════════════════

def market_is_open(ib: IB) -> bool:
    try:
        c = Contract(conId=HOURS_PROBE_CONID, exchange=EXCHANGE)
        cds = ib.reqContractDetails(c)
        if not cds:
            log.warning("Could not read trading hours; falling back to weekday check.")
            return datetime.date.today().weekday() < 5
        cd = cds[0]
        tz = ZoneInfo("Europe/Berlin")
        now = datetime.datetime.now(tz)
        today_key = now.strftime("%Y%m%d")
        for seg in cd.liquidHours.split(";"):
            if not seg or not seg.startswith(today_key):
                continue
            if "CLOSED" in seg:
                log.info(f"Exchange CLOSED today ({today_key}).")
                return False
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
        log.error(f"  {ticker}: price {price} is implausible — SKIPPING.")
        return False
    last = state.get("last_prices", {}).get(ticker)
    if last:
        move = abs(price - last) / last
        if move > MAX_PRICE_JUMP:
            log.error(f"  {ticker}: price moved {move:.0%} vs last (€{last:.2f}"
                      f"→€{price:.2f}) — looks wrong, SKIPPING.")
            return False
    return True


def get_prices(state: dict) -> dict | None:
    """Fetch + sanity-check all portfolio prices. Returns {ticker: price} or
    None if ANY price is bad (we never act on a partial price set)."""
    prices = {}
    for tk in PORTFOLIO:
        p = get_price_yahoo(tk)
        if not sane_price(tk, p, state):
            return None
        prices[tk] = p
    # Only commit last_prices once the whole set is good.
    for tk, p in prices.items():
        state.setdefault("last_prices", {})[tk] = p
    return prices


# ══════════════════════════════════════════════════════════════════════════════
# FUNDS / CONTRACTS
# ══════════════════════════════════════════════════════════════════════════════

def available_eur(ib: IB) -> float:
    vals = {(v.tag, v.currency): v.value
            for v in ib.accountValues() if v.account == ACCOUNT_ID}
    for tag in ("AvailableFunds", "TotalCashValue"):
        if (tag, "EUR") in vals:
            try:
                return float(vals[(tag, "EUR")])
            except ValueError:
                pass
    return 0.0


def qualify(ib: IB, ticker: str) -> Contract | None:
    c = Contract(conId=CONIDS[ticker], exchange=EXCHANGE)
    try:
        q = ib.qualifyContracts(c)
        return q[0] if q else None
    except Exception as e:
        log.error(f"  {ticker}: qualify error {e}")
        return None


def round_qty(x: float) -> float:
    """Round toward zero to the fractional-share increment."""
    f = 10 ** QTY_DECIMALS
    return math.floor(abs(x) * f) / f * (1 if x >= 0 else -1)


# ══════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT  (shared by tranche-buys and rebalance)
# ══════════════════════════════════════════════════════════════════════════════

def write_log(row: dict) -> None:
    fields = ["date", "event", "ticker", "action", "qty", "ref_price",
              "fill_price", "fill_qty", "status", "note"]
    exists = LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def place_order(ib: IB, contract: Contract, ticker: str, action: str,
                qty: float, ref_price: float, event: str,
                dry_run: bool) -> dict:
    """Place one BUY/SELL order, wait for fill, log it. Returns a result dict
    including the SIGNED filled quantity (+buy / -sell) for holdings update."""
    today = datetime.date.today().isoformat()
    sign = 1.0 if action == "BUY" else -1.0

    if dry_run or PAPER_TRADE:
        tag = "DRY-RUN" if dry_run else "PAPER"
        log.info(f"  [{tag}] would {action} {qty} {ticker} (~€{qty*ref_price:.2f})")
        write_log({"date": today, "event": event, "ticker": ticker,
                   "action": action, "qty": qty, "ref_price": ref_price,
                   "fill_price": ref_price, "fill_qty": qty, "status": tag,
                   "note": ""})
        return {"ticker": ticker, "status": tag, "filled": sign * qty,
                "fill_price": ref_price}

    trade = ib.placeOrder(contract, MarketOrder(action, qty, account=ACCOUNT_ID))
    log.info(f"  {ticker}: order {trade.order.orderId} submitted ({action} {qty})")

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

    fp = trade.orderStatus.avgFillPrice or ref_price
    fq = trade.orderStatus.filled or 0.0
    note = ""
    if status == "REJECTED":
        logs = [le.message for le in trade.log] if trade.log else []
        note = "; ".join(logs[-2:])
        log.error(f"  {ticker}: REJECTED — {note}")
    else:
        log.info(f"  {ticker}: {status}  filled {fq}/{qty}  avg €{fp:.4f}")

    write_log({"date": today, "event": event, "ticker": ticker, "action": action,
               "qty": qty, "ref_price": ref_price, "fill_price": fp,
               "fill_qty": fq, "status": status, "note": note})

    return {"ticker": ticker, "status": status, "filled": sign * fq,
            "fill_price": fp}


def apply_fills(state: dict, results: list[dict]) -> None:
    """Update its tracked holdings by the signed filled quantities, then save."""
    for r in results:
        if r["status"] in ("FILLED", "PAPER", "DRY-RUN"):
            state["holdings"][r["ticker"]] = round(
                state["holdings"].get(r["ticker"], 0.0) + r["filled"], QTY_DECIMALS)
    save_state(state)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — INVEST A TRANCHE
# ══════════════════════════════════════════════════════════════════════════════

def do_tranche(ib: IB, state: dict, key: str, amount: float, prices: dict,
               avail: float, dry_run: bool) -> bool:
    """Invest one lump-sum tranche into the target weights. Returns True on
    success (all legs placed)."""
    n = len(PORTFOLIO)
    investable = amount - n * TX_FEE_PER_LEG - CASH_BUFFER
    if investable <= 0:
        log.error(f"Tranche €{amount:.2f} too small to cover fees+buffer.")
        return False

    # Funds guard: never spend more cash than is available (no margin).
    if not (dry_run or PAPER_TRADE) and amount > avail:
        log.error(f"Tranche €{amount:.2f} exceeds available cash €{avail:.2f}. "
                  f"Refusing to trade on margin. Abort.")
        notify("Buy-and-Hold Portfolio — ABORTED", "Tranche exceeds cash. No trade.")
        return False

    log.info(f"── Investing {key}: €{amount:.0f} into "
             f"{'/'.join(f'{int(w*100)}%' for w in PORTFOLIO.values())} ──")
    results = []
    for tk, w in PORTFOLIO.items():
        contract = qualify(ib, tk)
        if contract is None:
            log.error(f"  {tk}: could not qualify — SKIPPING.")
            results.append({"ticker": tk, "status": "REJECTED", "filled": 0.0,
                            "fill_price": prices[tk]})
            continue
        qty = round_qty(investable * w / prices[tk])
        if qty < 10 ** -QTY_DECIMALS:
            log.warning(f"  {tk}: budget too small — SKIPPING.")
            continue
        log.info(f"  {tk}: €{prices[tk]:.4f}  weight {w:.0%}  qty {qty}")
        results.append(place_order(ib, contract, tk, "BUY", qty, prices[tk],
                                   key, dry_run))

    apply_fills(state, results)
    bad = [r for r in results if r["status"] not in ("FILLED", "PAPER", "DRY-RUN")]
    if not dry_run and not bad:
        mark_event(state, key, {"amount": amount,
                                "holdings_after": dict(state["holdings"])})
    return not bad


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — ANNUAL REBALANCE (this strategy's slice only)
# ══════════════════════════════════════════════════════════════════════════════

def do_rebalance(ib: IB, state: dict, key: str, prices: dict, dry_run: bool) -> bool:
    """Restore target weights on its tracked holdings. Cash-neutral: the sells
    fund the buys. Operates ONLY on state['holdings'], never account totals."""
    holdings = state["holdings"]
    values = {tk: holdings.get(tk, 0.0) * prices[tk] for tk in PORTFOLIO}
    total = sum(values.values())
    if total <= 0:
        log.error("No tracked holdings to rebalance.")
        return False

    log.info(f"── Annual rebalance ({key}); slice value €{total:.2f} ──")
    targets = {tk: total * w for tk, w in PORTFOLIO.items()}
    drift_eur = {tk: targets[tk] - values[tk] for tk in PORTFOLIO}

    for tk in PORTFOLIO:
        log.info(f"  {tk}: now €{values[tk]:.2f} ({values[tk]/total:.1%})  "
                 f"target €{targets[tk]:.2f} ({PORTFOLIO[tk]:.0%})  "
                 f"Δ €{drift_eur[tk]:+.2f}")

    # Skip if everything is within the no-trade band.
    max_drift_frac = max(abs(drift_eur[tk]) / total for tk in PORTFOLIO)
    if max_drift_frac < REBALANCE_MIN_DRIFT:
        log.info(f"Max drift {max_drift_frac:.1%} < {REBALANCE_MIN_DRIFT:.0%} "
                 f"band — no rebalancing needed.")
        if not dry_run:
            mark_event(state, key, {"action": "no-trade",
                                    "holdings_after": dict(holdings)})
        return True

    # Execute SELLS first (to raise cash), then BUYS — keeps it cash-neutral
    # and avoids needing extra cash that belongs to the other strategy.
    sells = [(tk, drift_eur[tk]) for tk in PORTFOLIO if drift_eur[tk] < 0]
    buys  = [(tk, drift_eur[tk]) for tk in PORTFOLIO if drift_eur[tk] > 0]
    results = []

    for tk, d in sells:
        qty = round_qty(min(-d / prices[tk], holdings.get(tk, 0.0)))  # never oversell
        if qty < 10 ** -QTY_DECIMALS:
            continue
        contract = qualify(ib, tk)
        if contract is None:
            results.append({"ticker": tk, "status": "REJECTED", "filled": 0.0,
                            "fill_price": prices[tk]}); continue
        results.append(place_order(ib, contract, tk, "SELL", qty, prices[tk],
                                   key, dry_run))

    for tk, d in buys:
        qty = round_qty(d / prices[tk])
        if qty < 10 ** -QTY_DECIMALS:
            continue
        contract = qualify(ib, tk)
        if contract is None:
            results.append({"ticker": tk, "status": "REJECTED", "filled": 0.0,
                            "fill_price": prices[tk]}); continue
        results.append(place_order(ib, contract, tk, "BUY", qty, prices[tk],
                                   key, dry_run))

    apply_fills(state, results)
    bad = [r for r in results if r["status"] not in ("FILLED", "PAPER", "DRY-RUN")]
    if not dry_run and not bad:
        mark_event(state, key, {"action": "rebalanced",
                                "holdings_after": dict(state["holdings"])})
    return not bad


# ══════════════════════════════════════════════════════════════════════════════
# WHAT SHOULD THIS RUN DO?
# ══════════════════════════════════════════════════════════════════════════════

def decide_action(state: dict, today: datetime.date) -> tuple[str, str] | None:
    """Return (kind, event_key) for the single action this run should take, or
    None if there's nothing to do. Priority: pending tranche first, then the
    yearly rebalance."""
    # 1) Any tranche whose date has arrived and which isn't done yet?
    for key, t in TRANCHES.items():
        if not event_done(state, key) and today >= t["not_before"]:
            return ("tranche", key)
    # 2) Annual rebalance — only after BOTH tranches are in, in January, once/yr.
    both_in = all(event_done(state, k) for k in TRANCHES)
    if both_in and today.month == REBALANCE_MONTH:
        key = f"rebalance_{today.year}"
        if not event_done(state, key):
            return ("rebalance", key)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="IBKR buy-and-hold portfolio trader")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print actions but place nothing")
    ap.add_argument("--force", action="store_true",
                    help="ignore the once-per-event guard (re-run an event)")
    args = ap.parse_args()

    today = datetime.date.today()

    log.info("=" * 72)
    log.info(f"Buy-and-Hold Portfolio Trader  |  {today}  |  "
             f"{'DRY-RUN' if args.dry_run else ('PAPER' if PAPER_TRADE else 'LIVE')}")
    log.info("=" * 72)

    if abs(sum(PORTFOLIO.values()) - 1.0) > 1e-6:
        log.error(f"Weights sum to {sum(PORTFOLIO.values())}, must be 1.0. Abort.")
        sys.exit(1)

    state = load_state()

    action = decide_action(state, today)
    if action is None and not args.force:
        log.info("Nothing scheduled for this run "
                 f"(holdings: {state['holdings']}). Done.")
        sys.exit(0)
    if args.force and action is None:
        log.warning("--force given but nothing is pending; nothing to do.")
        sys.exit(0)

    kind, key = action
    log.info(f"Action this run: {kind.upper()}  ({key})")

    ib = IB()
    log.info(f"Connecting to IB at {HOST}:{PORT} (clientId={CLIENT_ID})...")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=20)
    except Exception as e:
        log.error(f"Connection failed: {e}. Is IB Gateway running with API on?")
        notify("Buy-and-Hold Portfolio — FAILED", "Could not connect to IB Gateway.")
        sys.exit(1)
    log.info(f"Connected. Account {ACCOUNT_ID}.")

    try:
        # Market must be genuinely open (unless previewing).
        if not args.dry_run and not market_is_open(ib):
            log.info("Market not open now — exiting without trading; "
                     "the next scheduled run will retry.")
            sys.exit(0)

        prices = get_prices(state)
        if prices is None:
            log.error("Price set incomplete/insane — refusing to act this run.")
            notify("Buy-and-Hold Portfolio — waiting", "Bad price data; will retry.")
            sys.exit(1)
        save_state(state)  # persist good last_prices

        avail = available_eur(ib)
        log.info(f"Available cash: €{avail:.2f}  |  "
                 f"tracked holdings: {state['holdings']}")

        if kind == "tranche":
            ok = do_tranche(ib, state, key, TRANCHES[key]["amount"],
                            prices, avail, args.dry_run)
        else:
            ok = do_rebalance(ib, state, key, prices, args.dry_run)

        # Summary
        val = sum(state["holdings"].get(tk, 0.0) * prices[tk] for tk in PORTFOLIO)
        log.info("=" * 72)
        log.info(f"{kind.upper()} {'OK' if ok else 'INCOMPLETE'}  |  "
                 f"slice value ≈ €{val:.2f}")
        for tk in PORTFOLIO:
            v = state["holdings"].get(tk, 0.0) * prices[tk]
            log.info(f"  {tk:<6} {state['holdings'].get(tk,0.0):>10.4f} sh  "
                     f"≈ €{v:.2f}  ({v/val:.1%})" if val > 0 else f"  {tk}")
        log.info(f"Logs: {LOG_FILE.name}, {STATE_FILE.name}")
        log.info("=" * 72)

        if args.dry_run:
            notify("Buy-and-Hold Portfolio — dry run", f"{kind} previewed.")
        elif ok:
            notify("Buy-and-Hold Portfolio — done",
                   f"{kind} complete. Slice ≈€{val:.0f}.")
        else:
            notify("Buy-and-Hold Portfolio — PARTIAL",
                   f"{kind} incomplete; check log.")
            sys.exit(2)

    finally:
        ib.disconnect()
        log.info("Disconnected from IBKR.")


if __name__ == "__main__":
    main()
