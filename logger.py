"""VALR carry logger — hourly snapshot of spread, depth, basis, funding.
Runs on GitHub Actions. Public endpoints only; no keys, ever."""

import os
import requests
import pandas as pd
from datetime import datetime, timezone

BASE = "https://api.valr.com"
PERP = "BTCUSDTPERP"
SPOT = "BTCUSDT"
LOG_FILE = "valr_carry_log.csv"


def take_snapshot():
    snap = {"utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    # Orderbook: spread + depth
    r = requests.get(f"{BASE}/v1/public/{PERP}/orderbook", timeout=20)
    ob = r.json()
    bids = pd.DataFrame(ob.get("Bids", ob.get("bids")))
    asks = pd.DataFrame(ob.get("Asks", ob.get("asks")))
    for side in (bids, asks):
        side["price"] = side["price"].astype(float)
        side["quantity"] = side["quantity"].astype(float)
    best_bid = bids["price"].max()
    best_ask = asks["price"].min()
    mid = (best_bid + best_ask) / 2
    snap["best_bid"] = best_bid
    snap["best_ask"] = best_ask
    snap["spread_pct"] = round((best_ask - best_bid) / mid * 100, 5)
    nb = bids[bids["price"] >= mid * 0.995]
    na = asks[asks["price"] <= mid * 1.005]
    snap["bid_depth_usd"] = round((nb["price"] * nb["quantity"]).sum())
    snap["ask_depth_usd"] = round((na["price"] * na["quantity"]).sum())

    # Latest funding settlement
    r = requests.get(f"{BASE}/v1/public/futures/funding/history",
                     params={"currencyPair": PERP, "limit": 1}, timeout=20)
    fr = r.json()
    if fr:
        snap["last_funding_rate"] = float(fr[0]["fundingRate"])
        snap["last_funding_time"] = fr[0]["fundingTime"]

    # Perp vs spot: the basis
    for name, pair in [("perp", PERP), ("spot", SPOT)]:
        r = requests.get(f"{BASE}/v1/public/{pair}/marketsummary", timeout=20)
        if r.status_code == 200:
            snap[f"{name}_last"] = float(r.json().get("lastTradedPrice", "nan"))
        else:
            snap[f"{name}_last"] = float("nan")
            snap[f"{name}_error"] = r.status_code
    if snap.get("perp_last") == snap.get("perp_last") and \
       snap.get("spot_last") == snap.get("spot_last"):        # NaN-safe check
        snap["basis_pct"] = round((snap["perp_last"] - snap["spot_last"])
                                  / snap["spot_last"] * 100, 4)
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
