#!/usr/bin/env python3
"""End-to-end v2 testnet session-play test for lgda-music (base-sepolia).

Same flow as test_purchase_v2.py, but for the $0.05 single session play
(v2.5.0): reads the PAYMENT-REQUIRED header from the /play 402, signs
the identical EIP-3009 authorization for 50000 atomic USDC, and sends
the v2 envelope in the PAYMENT-SIGNATURE header. Verifies the MP3
bytes are identical to the catalog file, that NO session token and NO
settlement/receipt headers are issued (a play grants nothing durable),
and that a follow-up request without payment gets a 402 again.

Needs a funded throwaway buyer wallet at /tmp/buyer_wallet.json
{"address": ..., "key": ...} — testnet only, never mainnet, never committed.
Fund it with test USDC from faucet.circle.com (Base Sepolia, no account).
Needs a venv with eth_account + requests (the old /tmp/x402-venv is
gone; recreate with: python -m venv /tmp/x402-venv &&
/tmp/x402-venv/bin/pip install eth-account requests).

IMPORTANT: run only AFTER v2.5.0 is deployed — the live site serves
2.4.0 until then and /play does not exist there yet. To test a local
server instead, set LGDA_BASE_URL=http://127.0.0.1:8000.

Usage: /tmp/x402-venv/bin/python scripts/test_play_v2.py [track_id]
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

import requests
from eth_account import Account

BASE_URL = os.environ.get("LGDA_BASE_URL", "https://lgda-music.fly.dev").rstrip("/")
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia USDC
CHAIN_ID = 84532
PAY_TO = "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
PLAY_PRICE = 50_000  # $0.05, 6 decimals

RPCS = [
    "https://sepolia.base.org",
    "https://base-sepolia-rpc.publicnode.com",
    "https://base-sepolia.blockpi.network/v1/rpc/public",
]

ROOT = Path(__file__).resolve().parent.parent


def usdc_balance(address: str):
    """balanceOf via eth_call; returns float USDC or None if no RPC works."""
    data = ("0x70a08231" + address[2:].lower().rjust(64, "0"))
    body = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": USDC, "data": data}, "latest"]}
    for rpc in RPCS:
        try:
            r = requests.post(rpc, json=body, timeout=20)
            res = r.json().get("result")
            if res:
                return int(res, 16) / 1e6
        except Exception as exc:
            print(f"  RPC {rpc} failed: {exc}")
    return None


def sign_payment_v2(acct, nonce: bytes, valid_before: int, required: dict):
    """Build the v2 PaymentPayload: the `accepted` entry is echoed
    verbatim from the server's PAYMENT-REQUIRED header; the EIP-3009
    authorization is identical to the /stream flow."""
    domain = {"name": "USDC", "version": "2", "chainId": CHAIN_ID,
              "verifyingContract": USDC}
    types = {"TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ]}
    msg = {"from": acct.address, "to": PAY_TO, "value": PLAY_PRICE,
           "validAfter": 0, "validBefore": valid_before, "nonce": nonce}
    signed = acct.sign_typed_data(domain, types, msg)
    # NOTE: the facilitator rejects the signature without the 0x prefix
    # ("invalid_exact_evm_signature"); with it, verification passes.
    auth = {"from": acct.address, "to": PAY_TO, "value": str(PLAY_PRICE),
            "validAfter": "0", "validBefore": str(valid_before),
            "nonce": "0x" + nonce.hex()}
    payload = {"x402Version": 2,
               "resource": required["resource"],
               "accepted": required["accepts"][0],
               # Real v2 clients echo the 402's extensions into the
               # payload; the facilitator only catalogs the bazaar
               # discovery info when it arrives here.
               "extensions": required.get("extensions", {}),
               "payload": {"signature": "0x" + signed.signature.hex(),
                           "authorization": auth}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def main():
    track_id = sys.argv[1] if len(sys.argv) > 1 else "midnight"
    wallet = json.loads(Path("/tmp/buyer_wallet.json").read_text())
    acct = Account.from_key(wallet["key"])
    session_id = f"e2e-play-v2-{int(time.time())}"
    print(f"buyer: {acct.address}")
    print(f"target: {BASE_URL}")
    print(f"track: {track_id}, session: {session_id}")

    # 0. the target must be on v2.5.0 (the /play endpoint does not
    #    exist on 2.4.0 or earlier).
    health = requests.get(f"{BASE_URL}/healthz", timeout=30).json()
    print(f"node version: {health['version']}")
    assert health["version"] >= "2.5.0", "deploy v2.5.0 first"

    bal = usdc_balance(acct.address)
    print(f"USDC balance: {bal if bal is not None else 'unknown (no RPC)'}")
    if bal is not None and bal < 0.05:
        print("FUNDING NEEDED: claim test USDC at faucet.circle.com "
              "(Base Sepolia) for the buyer address above.")
        return 1

    # 1. 402 check (no payment): v2 header + v1 body, $0.05
    r = requests.get(f"{BASE_URL}/play",
                     params={"track_id": track_id, "session_id": session_id},
                     timeout=30)
    assert r.status_code == 402, f"expected 402, got {r.status_code}: {r.text[:200]}"
    assert r.json()["x402Version"] == 1, "v1 body missing from dual-serve 402"
    assert r.json()["accepts"][0]["maxAmountRequired"] == "50000", "not $0.05"
    assert "PAYMENT-REQUIRED" in r.headers, "no PAYMENT-REQUIRED header"
    required = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
    assert required["x402Version"] == 2
    acc = required["accepts"][0]
    assert acc["network"] == "eip155:84532", acc
    assert acc["amount"] == "50000", acc
    assert acc["asset"] == USDC
    assert acc["payTo"] == PAY_TO
    assert "bazaar" in required.get("extensions", {}), "no bazaar extension in 402"
    baz = required["extensions"]["bazaar"]
    assert baz["info"]["input"]["queryParams"]["track_id"] == track_id
    print("1. 402 with v2 PAYMENT-REQUIRED header + v1 body + bazaar extension: OK")

    # 2. pay $0.05 (v2) — MP3 once, no token, no receipt headers
    nonce = secrets.token_bytes(32)
    header = sign_payment_v2(acct, nonce, int(time.time()) + 600, required)
    r = requests.get(f"{BASE_URL}/play",
                     params={"track_id": track_id, "session_id": session_id},
                     headers={"PAYMENT-SIGNATURE": header}, timeout=120)
    if r.status_code != 200:
        print(f"2. PAYMENT FAILED ({r.status_code}): {r.text[:500]}")
        return 1
    assert "X-LGDA-Session" not in r.headers, "a play must not issue a session token"
    assert "PAYMENT-RESPONSE" not in r.headers, "a play must not issue a settlement header"
    assert "X-PAYMENT-RESPONSE" not in r.headers, "a play must not issue a settlement header"
    assert r.headers.get("X-LGDA-Session-Play") == "single-use", "missing single-use header"
    assert "no ownership" in r.headers.get("X-LGDA-License", ""), "missing license terms header"
    out = Path(f"/tmp/{track_id}-played-v2.mp3")
    out.write_bytes(r.content)
    local = ROOT / "audio" / f"{track_id}.mp3"
    same = (hashlib.sha256(out.read_bytes()).hexdigest()
            == hashlib.sha256(local.read_bytes()).hexdigest())
    print(f"2. paid $0.05 (v2) + streamed {len(r.content)/1e6:.1f} MB; "
          f"matches local file: {same}; no token/receipt headers")
    assert same, "downloaded MP3 differs from the catalog file!"

    # 3. no reuse: the same session without payment gets a 402 again
    r = requests.get(f"{BASE_URL}/play",
                     params={"track_id": track_id, "session_id": session_id},
                     timeout=30)
    assert r.status_code == 402, f"expected 402 (no reuse), got {r.status_code}"
    print("3. no reuse — unpaid repeat request returns 402: OK")
    print("\nEND-TO-END v2 TESTNET SESSION PLAY ($0.05): PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
