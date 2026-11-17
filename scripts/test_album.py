#!/usr/bin/env python3
"""End-to-end album purchase test for lgda-music (base-sepolia).

Flow: 402 ($14.99) -> sign x402 authorization -> GET /stream for track 1
with album_id + X-PAYMENT -> MP3 + session token granting all 3 tracks ->
reuse token for tracks 2 and 3 -> POST /verify-session with album_id ->
signed receipt, verified against /.well-known/receipt-key.

Needs a funded throwaway buyer wallet at /tmp/buyer_wallet2.json
(testnet only, never mainnet, never committed).

Usage: /tmp/x402-venv/bin/python scripts/test_album.py [album_id]
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_purchase import usdc_balance, BASE_URL, PAY_TO, USDC, CHAIN_ID  # noqa: E402

ALBUM_PRICE = 14_990_000  # $14.99, 6 decimals
ROOT = Path(__file__).resolve().parent.parent


def sign_payment(acct, value, nonce, valid_before):
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
    msg = {"from": acct.address, "to": PAY_TO, "value": value,
           "validAfter": 0, "validBefore": valid_before, "nonce": nonce}
    signed = acct.sign_typed_data(domain, types, msg)
    auth = {"from": acct.address, "to": PAY_TO, "value": str(value),
            "validAfter": "0", "validBefore": str(valid_before),
            "nonce": "0x" + nonce.hex()}
    payload = {"x402Version": 1, "scheme": "exact", "network": "base-sepolia",
               "payload": {"signature": "0x" + signed.signature.hex(),
                           "authorization": auth}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def check_mp3(track_id, content):
    local = ROOT / "audio" / f"{track_id}.mp3"
    return (hashlib.sha256(content).hexdigest()
            == hashlib.sha256(local.read_bytes()).hexdigest())


def main():
    album_id = sys.argv[1] if len(sys.argv) > 1 else "black-foot-ronin-vol-two"
    catalog = json.loads((ROOT / "catalog.json").read_text())
    album = next(a for a in catalog["albums"] if a["album_id"] == album_id)
    track_ids = album["track_ids"]
    wallet = json.loads(Path("/tmp/buyer_wallet2.json").read_text())
    acct = Account.from_key(wallet["key"])
    session_id = f"e2e-album-{int(time.time())}"
    print(f"buyer: {acct.address}")
    print(f"album: {album['title']} {track_ids}, session: {session_id}")

    bal = usdc_balance(acct.address)
    print(f"USDC balance: {bal if bal is not None else 'unknown (no RPC)'}")
    if bal is not None and bal < 14.99:
        print("FUNDING NEEDED: claim 20 test USDC at faucet.circle.com "
              "(Base Sepolia) for the buyer address above.")
        return 1

    first = track_ids[0]
    params = {"track_id": first, "album_id": album_id,
              "session_id": session_id}
    # 1. 402 check
    r = requests.get(f"{BASE_URL}/stream", params=params, timeout=30)
    assert r.status_code == 402, f"expected 402, got {r.status_code}"
    assert r.json()["accepts"][0]["maxAmountRequired"] == str(ALBUM_PRICE), \
        "402 requirements are not the $14.99 album price"
    print("1. 402 without payment ($14.99 album price): OK")

    # 2. pay once, get track 1 + token
    nonce = secrets.token_bytes(32)
    header = sign_payment(acct, ALBUM_PRICE, nonce, int(time.time()) + 600)
    r = requests.get(f"{BASE_URL}/stream", params=params,
                     headers={"X-PAYMENT": header}, timeout=120)
    if r.status_code != 200:
        print(f"2. PAYMENT FAILED ({r.status_code}): {r.text[:500]}")
        return 1
    token = r.headers.get("X-LGDA-Session")
    assert token, "no X-LGDA-Session header on paid response"
    assert check_mp3(first, r.content), "track 1 MP3 differs from catalog file"
    token_payload = json.loads(base64.urlsafe_b64decode(
        token.split(".")[0] + "=="))
    assert sorted(token_payload["track_ids"]) == sorted(track_ids), \
        f"token grants {token_payload['track_ids']}, expected {track_ids}"
    print(f"2. paid $14.99 + streamed {first}; token grants all 3 tracks: OK")
    print(f"   payment tx: {token_payload.get('payment_tx')}")

    # 3. token reuse for the other tracks (no payment)
    for tid in track_ids[1:]:
        r = requests.get(
            f"{BASE_URL}/stream",
            params={"track_id": tid, "album_id": album_id,
                    "session_id": session_id},
            headers={"X-LGDA-Session": token}, timeout=60)
        assert r.status_code == 200, f"token reuse for {tid} failed: {r.status_code}"
        assert check_mp3(tid, r.content), f"{tid} MP3 differs from catalog file"
    print("3. session-token reuse for tracks 2 and 3 (no payment): OK")

    # 4. receipt
    req = {"session_id": session_id, "agent_id": "e2e-test",
           "session_token": token,
           "payment_tx": token_payload.get("payment_tx"),
           "payment_network": "base-sepolia", "payment_rail": "x402",
           "album_id": album_id,
           "pre": {"rmssd_ms": 110.0, "source": "rppg"},
           "post": {"rmssd_ms": 175.0, "source": "rppg"}}
    r = requests.post(f"{BASE_URL}/verify-session", json=req, timeout=60)
    assert r.status_code == 200, f"verify-session failed: {r.status_code}"
    body = r.json()
    assert body["receipt"]["track_ids"] == track_ids
    print(f"4. receipt issued for all 3 tracks; "
          f"rmssd delta: {body['receipt']['rmssd_delta_ms']} ms")

    # 5. verify the receipt signature
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
    print("\nEND-TO-END ALBUM PURCHASE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
