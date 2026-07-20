"""VALR carry logger v2 — hourly snapshot of spread, depth, basis, funding.
Runs on GitHub Actions. Public endpoints only; no keys, ever.
v2 change: basis computed from orderbook MIDs on both legs.
(v1 used lastTradedPrice; VALR's thin spot pair leaves stale prints,
which produced fake negative-basis spikes on 19 Jul.)"""

import os
import requests
import pandas as pd
from datetime import datetime, timezone

BASE = "https://api.valr.com"
PERP = "BTCUSDTPERP"
SPOT = "BTCUSDT"
LOG_FILE = "valr_carry_log.csv"


def book_mid(pair):
    """Orderbook mid for any pair. Returns NaN on failure rather than crashing."""
    r = requests.get(f"{BASE}/v1/public/{pair}/orderbook", timeout=20)
    if r.status_code != 200:
        return float("nan")
    o = r.json()
    b = pd.DataFrame(o.get("Bids", o.get("bids")))
    a = pd.DataFrame(o.get("Asks", o.get("asks")))
    if b.empty or a.empty:
        return float("nan")
    return (b["price"].astype(float).max() + a["price"].astype(float).min()) / 2


def take_snapshot():
    snap = {"utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # --- Perp orderbook: spread + depth ---
    r = requests.get(f"{BASE}/v1/public/{PERP}/orderbook", timeout=20)
    ob = r.json()
    bids = pd.DataFrame(ob.get("Bids", ob.get("bids")))
    asks = pd.DataFrame(ob.get("Asks", ob.get("asks")))
    for side in (bids, asks):
        side["price"] = side["price"].astype(float)
        side["quantity"] = side["quantity"].astype(float)
    best_bid = bids["price"].max()
    best_ask = asks["price"].min()
    perp_mid = (best_bid + best_ask) / 2
    snap["best_bid"] = best_bid
    snap["best_ask"] = best_ask
    snap["spread_pct"] = round((best_ask - best_bid) / perp_mid * 100, 5)
    nb = bids[bids["price"] >= perp_mid * 0.995]
    na = asks[asks["price"] <= perp_mid * 1.005]
    snap["bid_depth_usd"] = round((nb["price"] * nb["quantity"]).sum())
    snap["ask_depth_usd"] = round((na["price"] * na["quantity"]).sum())

    # --- Latest funding settlement ---
    r = requests.get(f"{BASE}/v1/public/futures/funding/history",
                     params={"currencyPair": PERP, "limit": 1}, timeout=20)
    fr = r.json()
    if fr:
        snap["last_funding_rate"] = float(fr[0]["fundingRate"])
        snap["last_funding_time"] = fr[0]["fundingTime"]

    # --- Basis from orderbook MIDs (v2: last-traded is stale on thin spot) ---
    spot_mid = book_mid(SPOT)
    snap["perp_mid"] = round(perp_mid, 1)
    snap["spot_mid"] = round(spot_mid, 1) if spot_mid == spot_mid else float("nan")
    if spot_mid == spot_mid:                      # NaN-safe: NaN != NaN
        snap["basis_pct"] = round((perp_mid - spot_mid) / spot_mid * 100, 4)

    return snap


def main():
    try:
        snap = take_snapshot()
    except Exception as e:
        snap = {"utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": str(e)[:200]}
    row = pd.DataFrame([snap])
    header_needed = not os.path.exists(LOG_FILE)
    row.to_csv(LOG_FILE, mode="a", header=header_needed, index=False)
    print(f"Logged: {snap.get('utc_time')} | spread {snap.get('spread_pct')} | "
          f"basis {snap.get('basis_pct')} | funding {snap.get('last_funding_rate')}")


if __name__ == "__main__":
    main()
