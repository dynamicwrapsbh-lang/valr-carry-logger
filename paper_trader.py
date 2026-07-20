"""VALR paper trader v1 — the thermostat, trading imaginary money at real prices.
Runs on GitHub Actions alongside the logger. Public endpoints only; no keys, ever.

Anatomy per run: wake -> recall (paper_state.json) -> observe (VALR funding + book)
-> decide (untouched thermostat: 30d avg >10% enter, <5% exit) -> act on paper
-> remember (state + paper_ledger.csv) -> die.

Honesty rules: maker fills are ASSUMED (flagged optimistic); funding accrues at
actual settled rates, negative hours included; basis recorded at every event."""

import json
import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone

BASE = "https://api.valr.com"
PERP = "BTCUSDTPERP"
STATE_FILE = "paper_state.json"
LEDGER_FILE = "paper_ledger.csv"

NOTIONAL_USD = 500.0          # paper size ~= eventual real size
ENTER_ABOVE = 10.0            # 30d avg annualized %, untouched from validation
EXIT_BELOW = 5.0
MAKER_FEE = 0.0               # VALR maker ~0 / small rebate; assume 0, verify at account opening
HOURS_PER_YEAR = 24 * 365


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_recent_funding(n_hours=720):
    """Last ~30 days of hourly settlements via skip-pagination (works within window)."""
    rows = []
    for skip in range(0, n_hours + 100, 100):
        r = requests.get(f"{BASE}/v1/public/futures/funding/history",
                         params={"currencyPair": PERP, "limit": 100, "skip": skip},
                         timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        time.sleep(0.5)
    df = pd.DataFrame(rows)
    df["fundingTime"] = pd.to_datetime(df["fundingTime"], format="ISO8601", utc=True)
    df["fundingRate"] = df["fundingRate"].astype(float)
    df = (df.drop_duplicates(subset="fundingTime")
            .sort_values("fundingTime")
            .reset_index(drop=True))
    return df.tail(n_hours)


def orderbook_top():
    r = requests.get(f"{BASE}/v1/public/{PERP}/orderbook", timeout=20)
    o = r.json()
    b = pd.DataFrame(o.get("Bids", o.get("bids")))
    a = pd.DataFrame(o.get("Asks", o.get("asks")))
    best_bid = b["price"].astype(float).max()
    best_ask = a["price"].astype(float).min()
    return best_bid, best_ask


def spot_mid():
    r = requests.get(f"{BASE}/v1/public/BTCUSDT/orderbook", timeout=20)
    if r.status_code != 200:
        return float("nan")
    o = r.json()
    b = pd.DataFrame(o.get("Bids", o.get("bids")))
    a = pd.DataFrame(o.get("Asks", o.get("asks")))
    if b.empty or a.empty:
        return float("nan")
    return (b["price"].astype(float).max() + a["price"].astype(float).min()) / 2


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"status": "FLAT", "round_trips_completed": 0,
            "cum_funding_usd": 0.0, "created": now_iso()}


def save_state(state):
    state["last_run"] = now_iso()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def append_ledger(row):
    df = pd.DataFrame([row])
    header = not os.path.exists(LEDGER_FILE)
    df.to_csv(LEDGER_FILE, mode="a", header=header, index=False)


def main():
    state = load_state()
    funding = fetch_recent_funding()
    avg_ann = funding["fundingRate"].mean() * HOURS_PER_YEAR * 100
    latest_time = funding["fundingTime"].max()

    best_bid, best_ask = orderbook_top()
    perp_mid = (best_bid + best_ask) / 2
    s_mid = spot_mid()
    basis = round((perp_mid - s_mid) / s_mid * 100, 4) if s_mid == s_mid else float("nan")

    event = {"utc_time": now_iso(), "avg_30d_ann_pct": round(avg_ann, 3),
             "basis_pct": basis, "perp_mid": round(perp_mid, 1)}

    if state["status"] == "IN":
        # Accrue actual settled funding since last accrual (short collects +rate)
        since = pd.Timestamp(state["last_accrued_time"])
        new = funding[funding["fundingTime"] > since]
        gained = float(new["fundingRate"].sum()) * NOTIONAL_USD
        state["cum_funding_usd"] = round(state["cum_funding_usd"] + gained, 4)
        state["last_accrued_time"] = latest_time.isoformat()

        if avg_ann < EXIT_BELOW:
            # EXIT: short covers -> assumed maker fill joining the bid
            fee = NOTIONAL_USD * MAKER_FEE
            state["round_trips_completed"] += 1
            event.update({"event": "EXIT", "assumed_fill": "maker@bid",
                          "fill_price": best_bid, "fee_usd": fee,
                          "settlements_accrued": len(new),
                          "funding_added_usd": round(gained, 4),
                          "cum_funding_usd": state["cum_funding_usd"],
                          "entry_time": state.get("entry_time"),
                          "round_trip_no": state["round_trips_completed"]})
            append_ledger(event)
            state = {"status": "FLAT",
                     "round_trips_completed": state["round_trips_completed"],
                     "cum_funding_usd": state["cum_funding_usd"],
                     "created": state.get("created")}
        else:
            event.update({"event": "ACCRUE", "settlements_accrued": len(new),
                          "funding_added_usd": round(gained, 4),
                          "cum_funding_usd": state["cum_funding_usd"],
                          "entry_basis_pct": state.get("entry_basis_pct")})
            append_ledger(event)

    else:  # FLAT
        if avg_ann > ENTER_ABOVE:
            # ENTER: open short -> assumed maker fill joining the ask
            fee = NOTIONAL_USD * MAKER_FEE
            inherited = state["round_trips_completed"] == 0 and "entry_time" not in state
            state.update({"status": "IN", "entry_time": now_iso(),
                          "entry_price": best_ask, "entry_basis_pct": basis,
                          "notional_usd": NOTIONAL_USD,
                          "last_accrued_time": latest_time.isoformat()})
            event.update({"event": "ENTER", "assumed_fill": "maker@ask",
                          "fill_price": best_ask, "fee_usd": fee,
                          "notional_usd": NOTIONAL_USD,
                          "inherited_feast": inherited})
            append_ledger(event)
        # FLAT + no signal: state remembers the visit, ledger stays clean

    save_state(state)
    print(f"[paper] {event.get('event', 'FLAT-HOLD')} | 30d avg {avg_ann:.2f}% | "
          f"basis {basis} | RTs {state['round_trips_completed']} | "
          f"cum funding ${state.get('cum_funding_usd', 0):.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        append_ledger({"utc_time": now_iso(), "event": "ERROR", "error": str(e)[:300]})
        print(f"[paper] ERROR: {e}")
