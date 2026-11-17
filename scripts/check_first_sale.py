#!/usr/bin/env python3
"""First-sale watch for the LGDA node (mainnet).

Checks the owner's Base mainnet USDC balance for an incoming payment
of exactly $5.99 (track) or $14.99 (album) and the x402scan listing
activity. State lives in ~/workspace/sale-watch-state.json (outside
the repo — never committed).

Exit codes: 0 = check completed (verdict printed), 2 = check failed
(all RPCs down). Prints one verdict line: SALE DETECTED / NO SALE /
BASELINE SET / CHECK FAILED, plus details.

Usage: python3 scripts/check_first_sale.py
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
OWNER_WALLET = "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
X402SCAN_URL = "https://www.x402scan.com/server/12a3231a-7a98-426c-9939-f7ca68d09af8"
STATE_FILE = Path.home() / "workspace" / "sale-watch-state.json"

RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.llamarpc.com",
]

SALE_AMOUNTS = {5.99: "track ($5.99)", 14.99: "album ($14.99)"}


def usdc_balance():
    data = "0x70a08231" + OWNER_WALLET[2:].lower().rjust(64, "0")
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC_MAINNET, "data": data}, "latest"]}
    for rpc in RPCS:
        try:
            r = requests.post(rpc, json=body, timeout=25)
            res = r.json().get("result")
            if res:
                return int(res, 16) / 1e6
        except Exception as exc:
            print(f"  RPC {rpc} failed: {exc}", file=sys.stderr)
    return None


def x402scan_activity():
    """Best-effort: pull the listing page and look for activity counts.
    Returns a dict of whatever it could parse, or {} if unparseable."""
    try:
        r = requests.get(X402SCAN_URL, timeout=30,
                         headers={"User-Agent": "lgda-sale-watch/1.0"})
        text = r.text
    except Exception as exc:
        print(f"  x402scan fetch failed: {exc}", file=sys.stderr)
        return {}
    out = {}
    # Next.js RSC payload embeds the activity as total_transactions,
    # total_amount, unique_buyers.
    for label in ("total_transactions", "total_amount", "unique_buyers"):
        # The RSC payload escapes quotes (\"total_transactions\":0).
        m = re.search(rf'"{label}\\"?:([0-9][0-9.]*)', text)
        if m:
            out[label] = m.group(1)
    return out


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return None


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    now = datetime.now(timezone.utc).isoformat()
    state = load_state()
    balance = usdc_balance()
    if balance is None:
        print("CHECK FAILED: could not read wallet balance (all RPCs down)")
        return 2
    activity = x402scan_activity()

    if state is None:
        save_state({"baseline_usdc": balance, "last_check": now,
                    "sale_detected": False, "x402scan": activity})
        print(f"BASELINE SET: wallet holds {balance:.2f} USDC on Base mainnet")
        return 0

    if state.get("sale_detected"):
        print("SALE ALREADY REPORTED: watch is done (remove the cron).")
        print(f"  recorded sale: {json.dumps(state.get('sale'))}")
        return 0

    baseline = state.get("baseline_usdc", 0.0)
    delta = round(balance - baseline, 2)
    state["last_check"] = now
    state["x402scan"] = activity
    if delta <= 0:
        save_state(state)
        print(f"NO SALE: balance {balance:.2f} USDC (baseline {baseline:.2f}); "
              f"x402scan activity: {activity or 'unparseable'}")
        return 0

    # Balance went up. A sale is exactly $5.99 or $14.99.
    matched = None
    for amount, label in SALE_AMOUNTS.items():
        if abs(delta - amount) < 0.01:
            matched = label
            break
    if matched:
        sale = {"amount_usdc": delta, "what": matched, "detected_at": now,
                "balance_after": balance, "x402scan": activity}
        state["sale_detected"] = True
        state["sale"] = sale
        state["baseline_usdc"] = balance
        save_state(state)
        print(f"SALE DETECTED: +{delta:.2f} USDC ({matched}); "
              f"wallet now {balance:.2f} USDC; x402scan: {activity or 'unparseable'}")
        return 0

    # Money arrived, but not a catalog amount — probably the owner moving
    # funds. Re-baseline so it is not re-reported.
    state["baseline_usdc"] = balance
    save_state(state)
    print(f"NO SALE: balance rose by {delta:.2f} USDC (not a catalog price — "
          f"likely the owner moving funds); re-baselined to {balance:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
