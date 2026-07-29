"""VALR carry logger v3 — hourly snapshot of spread, depth, basis, funding.
Runs on GitHub Actions. Public endpoints only; no keys, ever.

v2 change: basis computed from orderbook MIDs on both legs.
(v1 used lastTradedPrice; VALR's thin spot pair leaves stale prints,
which produced fake negative-basis spikes on 19 Jul.)

v3 change: additionally snapshot ETHUSDTPERP each run (spread, depth,
perp mid, basis vs ETHUSDT spot orderbook mid — same book_mid approach
as BTC). Rationale: the ETH carry validation (29 Jul) failed its
pre-registered liquidity bar on a SINGLE orderbook snapshot
(0.12148% vs the <=0.10% bar) and failed Gate B on round-trip count.
The registered revisit condition requires a 30d+ ETH spread time
series rather than another point-in-time reading, plus a re-check of
ETH's duty cycle. This logger feeds that condition.
ETH columns are strictly ADDITIVE: BTC values, column names and
formats are unchanged, and a failed ETH fetch blanks only the ETH
fields — it can never break the BTC row.

NOTE ON COLUMN NAMES: `perp_last` / `spot_last` are v1-era names kept
for backward compatibility. Since v2 they hold orderbook MIDs, not
last-traded prices. The names are deliberately not renamed so existing
consumers and the historical file stay valid.
"""

import os
import requests
import pandas as pd
from datetime import datetime, timezone

BASE = "https://api.valr.com"
PERP = "BTCUSDTPERP"
SPOT = "BTCUSDT"
ETH_PERP = "ETHUSDTPERP"
ETH_SPOT = "ETHUSDT"
LOG_FILE = "valr_carry_log.csv"

# Canonical column order. Rows are reindexed onto this before writing so a
# missing key (failed funding fetch, NaN basis, failed ETH block) writes a
# BLANK in the right column instead of silently shifting every later value
# one position left. Append mode is positional (header=False), so this order
# must match the CSV header exactly and may only ever be appended to.
COLUMNS = [
    "utc_time", "best_bid", "best_ask", "spread_pct",
    "bid_depth_usd", "ask_depth_usd",
    "last_funding_rate", "last_funding_time",
    "perp_last", "spot_last", "basis_pct",
    "eth_spread_pct", "eth_bid_depth_usd", "eth_ask_depth_usd",
    "eth_perp_mid", "eth_basis_pct",
]


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


def eth_snapshot():
    """ETH perp spread/depth/mid + basis vs ETH spot book mid (v3).

    Self-contained and fully isolated: every caller-visible failure mode
    returns blanks for the ETH fields only. Never raises to the BTC path.
    """
    out = {"eth_spread_pct": "", "eth_bid_depth_usd": "",
           "eth_ask_depth_usd": "", "eth_perp_mid": "", "eth_basis_pct": ""}

    r = requests.get(f"{BASE}/v1/public/{ETH_PERP}/orderbook", timeout=20)
    if r.status_code != 200:
        return out
    ob = r.json()
    bids = pd.DataFrame(ob.get("Bids", ob.get("bids")))
    asks = pd.DataFrame(ob.get("Asks", ob.get("asks")))
    if bids.empty or asks.empty:
        return out
    for side in (bids, asks):
        side["price"] = side["price"].astype(float)
        side["quantity"] = side["quantity"].astype(float)

    best_bid = bids["price"].max()
    best_ask = asks["price"].min()
    mid = (best_bid + best_ask) / 2
    out["eth_perp_mid"] = round(mid, 2)
    out["eth_spread_pct"] = round((best_ask - best_bid) / mid * 100, 5)

    nb = bids[bids["price"] >= mid * 0.995]
    na = asks[asks["price"] <= mid * 1.005]
    out["eth_bid_depth_usd"] = round((nb["price"] * nb["quantity"]).sum())
    out["eth_ask_depth_usd"] = round((na["price"] * na["quantity"]).sum())

    eth_spot_mid = book_mid(ETH_SPOT)
    if eth_spot_mid == eth_spot_mid:               # NaN-safe: NaN != NaN
        out["eth_basis_pct"] = round((mid - eth_spot_mid) / eth_spot_mid * 100, 4)

    return out


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
    # Keys are the v1-era CSV column names; the values are MIDs (see docstring).
    spot_mid = book_mid(SPOT)
    snap["perp_last"] = round(perp_mid, 1)
    snap["spot_last"] = round(spot_mid, 1) if spot_mid == spot_mid else float("nan")
    if spot_mid == spot_mid:                      # NaN-safe: NaN != NaN
        snap["basis_pct"] = round((perp_mid - spot_mid) / spot_mid * 100, 4)

    # --- ETH leg (v3). Isolated: any failure blanks ETH only, never BTC. ---
    try:
        snap.update(eth_snapshot())
    except Exception as e:
        print(f"ETH snapshot failed (BTC row unaffected): {str(e)[:200]}")

    return snap


def main():
    try:
        snap = take_snapshot()
    except Exception as e:
        snap = {"utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": str(e)[:200]}
        print(f"Snapshot failed: {snap['error']}")
    # Reindex onto the canonical order so every row has the same field count
    # in the same positions, whatever succeeded or failed above.
    row = pd.DataFrame([snap]).reindex(columns=COLUMNS)
    header_needed = not os.path.exists(LOG_FILE)
    row.to_csv(LOG_FILE, mode="a", header=header_needed, index=False)
    print(f"Logged: {snap.get('utc_time')} | spread {snap.get('spread_pct')} | "
          f"basis {snap.get('basis_pct')} | funding {snap.get('last_funding_rate')} | "
          f"eth_spread {snap.get('eth_spread_pct')} | eth_basis {snap.get('eth_basis_pct')}")


if __name__ == "__main__":
    main()
