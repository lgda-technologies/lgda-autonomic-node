#!/usr/bin/env python3
"""End-to-end v2 testnet purchase test for lgda-music (base-sepolia).

Same flow as test_purchase.py (v1), but speaks x402 v2: reads the
PAYMENT-REQUIRED header from the 402, signs the identical EIP-3009
authorization, and sends the v2 envelope in the PAYMENT-SIGNATURE
header. Verifies the PAYMENT-RESPONSE header, the MP3 bytes, session
token reuse, and the signed receipt.

Needs a funded throwaway buyer wallet at /tmp/buyer_wallet.json
{"address": ..., "key": ...} — testnet only, never mainnet, never committed.
Fund it with 20 test USDC from faucet.circle.com (Base Sepolia, no account).

Usage: /tmp/x402-venv/bin/python scripts/test_purchase_v2.py [track_id]
"""
import base64
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

import requests
from eth_account import Account

BASE_URL = "https://lgda-music.fly.dev"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"  # Base Sepolia USDC
CHAIN_ID = 84532
PAY_TO = "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
TRACK_PRICE = 5_990_000  # $5.99, 6 decimals

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
    authorization is identical to v1."""
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
    msg = {"from": acct.address, "to": PAY_TO, "value": TRACK_PRICE,
           "validAfter": 0, "validBefore": valid_before, "nonce": nonce}
    signed = acct.sign_typed_data(domain, types, msg)
    # NOTE: the facilitator rejects the signature without the 0x prefix
    # ("invalid_exact_evm_signature"); with it, verification passes.
    auth = {"from": acct.address, "to": PAY_TO, "value": str(TRACK_PRICE),
            "validAfter": "0", "validBefore": str(valid_before),
            "nonce": "0x" + nonce.hex()}
    payload = {"x402Version": 2,
               "resource": required["resource"],
               "accepted": required["accepts"][0],
               "payload": {"signature": "0x" + signed.signature.hex(),
                           "authorization": auth}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def main():
    track_id = sys.argv[1] if len(sys.argv) > 1 else "midnight"
    wallet = json.loads(Path("/tmp/buyer_wallet.json").read_text())
    acct = Account.from_key(wallet["key"])
    session_id = f"e2e-v2-{int(time.time())}"
    print(f"buyer: {acct.address}")
    print(f"track: {track_id}, session: {session_id}")

    bal = usdc_balance(acct.address)
    print(f"USDC balance: {bal if bal is not None else 'unknown (no RPC)'}")
    if bal is not None and bal < 5.99:
        print("FUNDING NEEDED: claim 20 test USDC at faucet.circle.com "
              "(Base Sepolia) for the buyer address above.")
        return 1

    # 1. 402 check (no payment): v2 header + v1 body
    r = requests.get(f"{BASE_URL}/stream",
                     params={"track_id": track_id, "session_id": session_id},
                     timeout=30)
    assert r.status_code == 402, f"expected 402, got {r.status_code}: {r.text[:200]}"
    assert r.json()["x402Version"] == 1, "v1 body missing from dual-serve 402"
    assert "PAYMENT-REQUIRED" in r.headers, "no PAYMENT-REQUIRED header"
    required = json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
    assert required["x402Version"] == 2
    acc = required["accepts"][0]
    assert acc["network"] == "eip155:84532", acc
    assert acc["amount"] == "5990000", acc
    assert acc["asset"] == USDC
    assert acc["payTo"] == PAY_TO
    print("1. 402 with v2 PAYMENT-REQUIRED header + v1 body: OK")

    # 2. pay (v2)
    nonce = secrets.token_bytes(32)
    header = sign_payment_v2(acct, nonce, int(time.time()) + 600, required)
    r = requests.get(f"{BASE_URL}/stream",
                     params={"track_id": track_id, "session_id": session_id},
                     headers={"PAYMENT-SIGNATURE": header}, timeout=120)
    if r.status_code != 200:
        print(f"2. PAYMENT FAILED ({r.status_code}): {r.text[:500]}")
        return 1
    token = r.headers.get("X-LGDA-Session")
    assert token, "no X-LGDA-Session header on paid response"
    assert "PAYMENT-RESPONSE" in r.headers, "no PAYMENT-RESPONSE header"
    resp = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert resp["success"] is True, resp
    assert resp["transaction"].startswith("0x"), resp
    assert resp["network"] == "eip155:84532", resp
    print(f"   settlement tx: {resp['transaction']}")
    out = Path(f"/tmp/{track_id}-purchased-v2.mp3")
    out.write_bytes(r.content)
    local = ROOT / "audio" / f"{track_id}.mp3"
    same = (hashlib.sha256(out.read_bytes()).hexdigest()
            == hashlib.sha256(local.read_bytes()).hexdigest())
    print(f"2. paid (v2) + streamed {len(r.content)/1e6:.1f} MB; "
          f"matches local file: {same}")
    assert same, "downloaded MP3 differs from the catalog file!"

    # 3. session token reuse (no payment)
    r = requests.get(f"{BASE_URL}/stream",
                     params={"track_id": track_id, "session_id": session_id},
                     headers={"X-LGDA-Session": token}, timeout=30)
    assert r.status_code == 200, f"token reuse failed: {r.status_code}"
    print("3. session-token reuse (no payment): OK")
    token_payload = json.loads(base64.urlsafe_b64decode(
        token.split(".")[0] + "=="))
    payment_tx = token_payload.get("payment_tx")
    print(f"   payment tx: {payment_tx}")

    # 4. receipt
    req = {"session_id": session_id, "agent_id": "e2e-test-v2",
           "session_token": token, "payment_tx": payment_tx,
           "payment_network": "eip155:84532", "payment_rail": "x402-v2",
           "track_id": track_id,
           "pre": {"rmssd_ms": 120.0, "source": "rppg"},
           "post": {"rmssd_ms": 180.5, "source": "rppg"}}
    r = requests.post(f"{BASE_URL}/verify-session", json=req, timeout=30)
    assert r.status_code == 200, f"verify-session failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    print(f"4. receipt issued; rmssd delta: "
          f"{body['receipt']['rmssd_delta_ms']} ms")

    # 5. verify the receipt signature against the published key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey)
    key = requests.get(f"{BASE_URL}/.well-known/receipt-key",
                       timeout=30).json()
    pub = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(key["public_key_b64"]))
    canon = json.dumps(body["receipt"], sort_keys=True,
                       separators=(",", ":")).encode()
    pub.verify(base64.b64decode(body["signature_b64"]), canon)
    print("5. receipt signature verifies against published key: OK")
    print("\nEND-TO-END v2 TESTNET PURCHASE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
