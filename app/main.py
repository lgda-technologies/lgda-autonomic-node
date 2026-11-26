"""
LGDA Autonomic Pacing Node — v2.5.0.

What this is: a machine-readable front door for LGDA Technologies'
wellness music catalog (https://lgda-technologies.base44.app).
AI agents discover it via the /mcp manifest, pay per track or album
with x402 (USDC), and receive the real MP3.

What changed from v1 (see README.md for the full list):
- v1 generated a synthetic paced-breathing tone at $0.05/stream.
  v2 sells the REAL 27-track catalog: $5.99/track, $14.99/album.
  It NEVER generates a synthetic tone as a substitute. If the MP3
  for a track has not been loaded into ./audio/, /stream returns
  503 "audio file not yet loaded for this track".
- v1 returned {"detail": "Not Found"} at GET /. v2 serves a real
  homepage there explaining the node.
- v2.1.0 speaks x402 v1 AND v2 (dual-serve): the 402 carries the
  canonical v2 PaymentRequired object in the PAYMENT-REQUIRED header
  and the v1 object in the body; payments are accepted on either the
  PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1) header. The EIP-3009
  authorization itself is identical in both versions.
- v2.2.0 adds the x402 `bazaar` discovery extension to the v2 402,
  so facilitators can catalog the /stream endpoint (HTTP method,
  query params, MP3 output) in a discovery index.
- v2.3.0 fixes the 402's EIP-712 domain name per network: mainnet
  USDC's domain name is "USD Coin", the base-sepolia test token's
  is "USDC" (both verified against the on-chain DOMAIN_SEPARATOR).
  v2.2.0 served "USDC" on both networks, so a mainnet payer's
  EIP-3009 signature would not have verified and settlement would
  have failed. Caught by PayAPI Market's listing check, 2026-11-19.
- v2.4.0 is the findability + probe-compat release. /stream returns
  the x402 402 BEFORE parameter validation: bare /stream used to
  422 (FastAPI validated track_id + session_id first), which broke
  probe-based directories and the x402gle audition
  (discovery_failed / not_402). The payment flow is now
  verify -> validate -> settle, so a payment for an unknown track,
  a missing session_id, or unloaded audio is verified but never
  settled. New discovery surfaces: the /.well-known/x402 (+ .json)
  manifest, /.well-known/x402-trust.txt (x402 Trust provider key),
  the SEP-2127 server card (/mcp/server-card) + AI Catalog
  (/.well-known/ai-catalog.json, /.well-known/ard.json), MCP tool
  annotations, and OpenAPI x-payment-info / x-guidance for x402gle.
- v2.5.0 adds a $0.05 per-session play: GET /play streams one
  track once for 50000 atomic USDC, with no ownership, no file
  retention, no session token and no signed receipt. It is a
  separate product from the $5.99 track / $14.99 album purchase
  (ownership). Same 402-before-validation rule and the same
  verify -> validate -> settle invariant as /stream; the 402
  carries the per-network EIP-712 domain name.

Wording: "wellness music". Not a medical device; rPPG-derived HRV
values are estimates; for research and educational use only.
Never "clinically calibrated", never diagnose/treat claims.

Entitlement model (simple):
- A track purchase pays $5.99 and entitles the buyer to that one
  track. An album purchase pays $14.99 and entitles the buyer to
  all 3 tracks on that album.
- A session play pays $0.05 and is NOT a purchase: it streams one
  track once, in the buying agent's session only. No ownership,
  no file retention, no redistribution, no session token, no
  signed receipt. Ownership is the $5.99 track / $14.99 album
  purchase only.
- After a successful x402 payment (verify + settle via the
  facilitator), the node issues a signed session token
  (X-LGDA-Session) listing the entitled track_ids. Present that
  token (or a fresh X-PAYMENT) to GET /stream for each track.
- The signed receipt from POST /verify-session echoes the
  track_ids and the payment reference.
"""

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

try:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
except ImportError:  # pragma: no cover - cryptography is in requirements.txt
    ec = None  # type: ignore
    Ed25519PrivateKey = None  # type: ignore

# ---------------------------------------------------------------- config

ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = Path(os.environ.get("AUDIO_DIR", ROOT / "audio"))
BASE_URL = os.environ.get("BASE_URL", "https://lgda-music.fly.dev").rstrip("/")
X402_NETWORK = os.environ.get("X402_NETWORK", "base-sepolia")
# NOTE: owner must confirm this wallet before mainnet. It is the payTo
# address carried over from v1 (base-sepolia testnet). Do not assume it
# is the right mainnet wallet — see README "Open questions".
PAY_TO_ADDRESS = os.environ.get(
    "PAY_TO_ADDRESS", "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
)
X402_FACILITATOR_URL = os.environ.get(
    "X402_FACILITATOR_URL", "https://x402.org/facilitator"
)
RECEIPT_KEY_FILE = Path(
    os.environ.get("RECEIPT_KEY_FILE", ROOT / "data" / "receipt_key.json")
)
# P-256 keypair for x402 Trust provider verification (see TrustKey).
TRUST_KEY_FILE = Path(
    os.environ.get("X402_TRUST_KEY_FILE", ROOT / "data" / "x402_trust_key.json")
)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

DISCLAIMER = (
    "Wellness music. Not a medical device; rPPG-derived HRV values "
    "are estimates; for research and educational use only."
)

# USDC asset (6 decimals) per x402 network.
USDC_ASSETS = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}
# x402 v2 identifies networks as CAIP-2 ids (v1 used plain names).
CAIP2_NETWORKS = {
    "base-sepolia": "eip155:84532",
    "base": "eip155:8453",
}
# EIP-712 domain name of the USDC contract per network. The two
# contracts differ: mainnet USDC's domain name is "USD Coin"
# (verified against the on-chain DOMAIN_SEPARATOR, 2026-11-19),
# while the base-sepolia test token's is "USDC". The 402's extra.name
# must match the contract's domain, or the payer's EIP-3009
# signature will not verify and settlement fails. Do not collapse
# this into a single constant: the testnet/mainnet difference is
# exactly what v2.2.0 got wrong.
EIP712_DOMAIN_NAMES = {
    "base-sepolia": "USDC",
    "base": "USD Coin",
}
TRACK_PRICE_USD = 5.99
ALBUM_PRICE_USD = 14.99
TRACK_PRICE_ATOMIC = 5_990_000  # $5.99, 6-decimal USDC
ALBUM_PRICE_ATOMIC = 14_990_000  # $14.99, 6-decimal USDC
SESSION_PLAY_PRICE_USD = 0.05
SESSION_PLAY_PRICE_ATOMIC = 50_000  # $0.05, 6-decimal USDC
# Paid measurement verification uses the established $0.05 micro price:
# one signed HRV/RMSSD outcome receipt, no license granted.
MEASUREMENT_VERIFY_PRICE_USD = SESSION_PLAY_PRICE_USD
MEASUREMENT_VERIFY_PRICE_ATOMIC = SESSION_PLAY_PRICE_ATOMIC
SESSION_TOKEN_TTL_S = 24 * 3600

# ---------------------------------------------------------------- catalog

with open(ROOT / "catalog.json") as f:
    CATALOG = json.load(f)
with open(ROOT / "sessions.json") as f:
    SESSIONS_DOC = json.load(f)

TRACKS = {t["track_id"]: t for t in CATALOG["tracks"]}
ALBUMS = {a["album_id"]: a for a in CATALOG["albums"]}
assert len(TRACKS) == 27 and len(ALBUMS) == 9

# ---------------------------------------------------------------- keys

def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical_json(obj: Any) -> bytes:
    """Canonical form for signing: sorted keys, compact, UTF-8 (as v1)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ReceiptKey:
    """Ed25519 receipt/signing key.

    Loaded from RECEIPT_PRIVATE_KEY_B64 (base64 raw 32-byte seed) if set,
    else from RECEIPT_KEY_FILE, else generated on first run and persisted
    to that file with mode 0600. The private key is NEVER committed:
    data/ is git-ignored (see .gitignore) and in production the key
    should come from a Fly secret instead.
    """

    def __init__(self) -> None:
        if Ed25519PrivateKey is None:
            raise RuntimeError("cryptography package is required")
        seed_b64 = os.environ.get("RECEIPT_PRIVATE_KEY_B64", "")
        if seed_b64:
            self.private = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(seed_b64)
            )
        elif RECEIPT_KEY_FILE.exists():
            data = json.loads(RECEIPT_KEY_FILE.read_text())
            self.private = Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(data["private_key_b64"])
            )
        else:
            self.private = Ed25519PrivateKey.generate()
            RECEIPT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            seed = self.private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            RECEIPT_KEY_FILE.write_text(
                json.dumps({"private_key_b64": _b64(seed), "note": "PRIVATE - do not commit"}, indent=2)
            )
            os.chmod(RECEIPT_KEY_FILE, 0o600)
        self.public = self.private.public_key()
        pub_raw = self.public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.key_id = "ed25519:" + hashlib.sha256(pub_raw).hexdigest()[:16]
        self.public_key_b64 = _b64(pub_raw)
        self.jwk = {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": _b64url(pub_raw),
            "use": "sig",
            "key_id": self.key_id,
        }

    def sign(self, obj: Any) -> str:
        return _b64(self.private.sign(canonical_json(obj)))

    def verify(self, obj: Any, signature_b64: str) -> bool:
        try:
            self.public.verify(base64.b64decode(signature_b64), canonical_json(obj))
            return True
        except Exception:
            return False


RECEIPT_KEY = ReceiptKey()


class TrustKey:
    """P-256 keypair for x402 Trust provider verification.

    x402 Trust (x402-trust.com) verifies a provider by a public key
    published at /.well-known/x402-trust.txt plus a confirmed email.
    The file format, verified against a live provider file
    (mcp.zeamprism.com, 2026-11-19), is one line:
        x402-trust-verification=v1:<base64url, unpadded, DER SPKI>
    where the key is P-256 (the example decodes to a 91-byte SPKI
    starting 30 59 30 13). The private key authorizes changes to the
    verification (removal included), so it must be kept safe: set
    X402_TRUST_PRIVATE_KEY_B64 (base64 of the raw 32-byte P-256
    private scalar) as a Fly secret so the key survives redeploys,
    exactly like the receipt key. The node itself only ever serves
    the PUBLIC key; the private key is used by the owner, off-node,
    when claiming or changing the verification with x402 Trust.
    """

    def __init__(self) -> None:
        if ec is None:
            raise RuntimeError("cryptography package is required")
        seed_b64 = os.environ.get("X402_TRUST_PRIVATE_KEY_B64", "")
        if seed_b64:
            scalar = int.from_bytes(base64.b64decode(seed_b64), "big")
            self.private = ec.derive_private_key(scalar, ec.SECP256R1())
        elif TRUST_KEY_FILE.exists():
            data = json.loads(TRUST_KEY_FILE.read_text())
            scalar = int.from_bytes(base64.b64decode(data["private_key_b64"]), "big")
            self.private = ec.derive_private_key(scalar, ec.SECP256R1())
        else:
            self.private = ec.generate_private_key(ec.SECP256R1())
            TRUST_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
            scalar = self.private.private_numbers().private_value.to_bytes(32, "big")
            TRUST_KEY_FILE.write_text(
                json.dumps({"private_key_b64": _b64(scalar), "note": "PRIVATE - do not commit"}, indent=2)
            )
            os.chmod(TRUST_KEY_FILE, 0o600)
        self.public = self.private.public_key()
        spki_der = self.public.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.verification_value = "v1:" + _b64url(spki_der)

    def trust_txt(self) -> str:
        return f"x402-trust-verification={self.verification_value}\n"


TRUST_KEY = TrustKey()

# ---------------------------------------------------------------- app

app = FastAPI(
    title="LGDA Autonomic Pacing Node",
    description=(
        "x402-paywalled wellness music catalog (27 tracks, 9 albums) "
        "with MCP discovery and signed HRV session receipts. "
        + DISCLAIMER
    ),
    version="2.5.0",
    contact={"name": "LGDA Technologies", "email": "solheefinery@gmail.com"},
)


_orig_openapi = app.openapi


def custom_openapi():
    """OpenAPI with the x-guidance hint x402gle's discovery prefers
    (see https://www.x402gle.com/agent.md): the document itself tells
    an agent what the API is for and how to pay for it."""
    schema = _orig_openapi()
    schema.setdefault("info", {})["x-guidance"] = (
        "AI agents: browse the LGDA wellness music catalog free via "
        "/mcp (list_catalog, get_track_evidence), then buy a track "
        "($5.99 USDC) or an album ($14.99 USDC, all 3 tracks) with "
        "x402 on GET /stream, or play one track once in a session "
        "($0.05 USDC, no ownership) on GET /play. An unpaid /stream "
        "or /play request returns HTTP 402 with the payment "
        "challenge; retry with a PAYMENT-SIGNATURE (x402 v2) or "
        "X-PAYMENT (v1) header to receive the MP3."
    )
    return schema


app.openapi = custom_openapi

# ---------------------------------------------------------------- helpers

def usdc_asset(network: str) -> str:
    try:
        return USDC_ASSETS[network]
    except KeyError:
        raise HTTPException(500, f"Unsupported X402_NETWORK: {network}")


def caip2_network(network: str) -> str:
    try:
        return CAIP2_NETWORKS[network]
    except KeyError:
        raise HTTPException(500, f"Unsupported X402_NETWORK: {network}")


def eip712_domain_name(network: str) -> str:
    try:
        return EIP712_DOMAIN_NAMES[network]
    except KeyError:
        raise HTTPException(500, f"Unsupported X402_NETWORK: {network}")


def x402_accepts(price_atomic: int, resource: str, description: str) -> dict:
    """v1 PaymentRequirements (kept for v1 clients)."""
    return {
        "scheme": "exact",
        "network": X402_NETWORK,
        "maxAmountRequired": str(price_atomic),
        "resource": resource,
        "description": description,
        "mimeType": "audio/mpeg",
        "payTo": PAY_TO_ADDRESS,
        "maxTimeoutSeconds": 120,
        "asset": usdc_asset(X402_NETWORK),
        "extra": {"name": eip712_domain_name(X402_NETWORK), "version": "2"},
    }


def x402_accepts_v2(price_atomic: int) -> dict:
    """v2 PaymentRequirements: CAIP-2 network, `amount` (not
    maxAmountRequired); resource/description live on the
    PaymentRequired.resource object instead."""
    return {
        "scheme": "exact",
        "network": caip2_network(X402_NETWORK),
        "amount": str(price_atomic),
        "asset": usdc_asset(X402_NETWORK),
        "payTo": PAY_TO_ADDRESS,
        "maxTimeoutSeconds": 120,
        "extra": {"name": eip712_domain_name(X402_NETWORK), "version": "2"},
    }


def bazaar_extension(resource: str) -> dict:
    """x402 v2 `bazaar` discovery extension for a paywalled endpoint
    (/stream or /play).

    Declares the endpoint's HTTP method, query parameters and output
    format so a facilitator can catalog it in a discovery index
    (e.g. Coinbase Bazaar) when a payment settles. Per the spec at
    specs/extensions/bazaar.md in coinbase/x402: `info` carries the
    discovery data, `schema` is the Draft 2020-12 JSON Schema the
    facilitator validates `info` against. The queryParams are the
    concrete values from this request's resource URL, so a browsing
    agent sees exactly how to call the endpoint.
    """
    params = parse_qs(urlparse(resource).query)
    query_params: dict[str, str] = {}
    if "track_id" in params:
        query_params["track_id"] = params["track_id"][0]
    if "album_id" in params:
        query_params["album_id"] = params["album_id"][0]
    query_params["session_id"] = params.get("session_id", ["your-session-id"])[0]
    return {
        "info": {
            "input": {
                "type": "http",
                "method": "GET",
                "queryParams": query_params,
            },
            "output": {
                "type": "binary",
                "format": "audio/mpeg",
            },
        },
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["GET", "HEAD", "DELETE"]},
                        "queryParams": {
                            "type": "object",
                            "properties": {k: {"type": "string"} for k in query_params},
                            "required": list(query_params.keys()),
                        },
                        "headers": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["type", "method"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "format": {"type": "string"},
                    },
                    "required": ["type"],
                },
            },
            "required": ["input"],
        },
    }


def x402_payment_required_v2(price_atomic: int, resource: str, description: str) -> dict:
    """v2 PaymentRequired object (goes in the PAYMENT-REQUIRED header)."""
    return {
        "x402Version": 2,
        "error": "PAYMENT-SIGNATURE header is required",
        "resource": {
            "url": resource,
            "description": description,
            "mimeType": "audio/mpeg",
            "serviceName": "LGDA Music",
            "tags": ["music", "wellness", "hrv"],
            "iconUrl": f"{BASE_URL}/favicon.ico",
        },
        "accepts": [x402_accepts_v2(price_atomic)],
        "extensions": {"bazaar": bazaar_extension(resource)},
    }


def x402_402(price_atomic: int, resource: str, description: str) -> JSONResponse:
    """Dual-serve 402: canonical v2 PaymentRequired in the
    PAYMENT-REQUIRED header (base64 JSON), v1 object in the body.
    v2 clients read the header and ignore the body; v1 clients
    parse the body and ignore the header. Verified pattern per the
    x402 v2 HTTP transport spec."""
    return JSONResponse(
        status_code=402,
        content={
            "x402Version": 1,
            "error": "X-PAYMENT header is required",
            "accepts": [x402_accepts(price_atomic, resource, description)],
        },
        headers={
            "PAYMENT-REQUIRED": _b64(
                json.dumps(
                    x402_payment_required_v2(price_atomic, resource, description),
                    separators=(",", ":"),
                ).encode()
            )
        },
    )


def _facilitator_post(path: str, payload: dict) -> dict:
    # NOTE: httpx, not urllib — the facilitator sits behind Cloudflare,
    # which 403s urllib's default "Python-urllib/3.12" User-Agent.
    # Verified 2026-11-17: this UA passes, urllib's does not.
    with httpx.Client(timeout=15, headers={
            "User-Agent": "lgda-music-node/2.5.0"}) as client:
        resp = client.post(X402_FACILITATOR_URL.rstrip("/") + path,
                           json=payload)
        resp.raise_for_status()
        return resp.json()


def _decode_payment_header(payment_header: str) -> dict:
    """Decode a PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1) header to the
    payment payload object. x402 clients send base64-encoded JSON;
    accept raw JSON too.
    """
    try:
        return json.loads(base64.b64decode(payment_header).decode())
    except Exception:
        return json.loads(payment_header)


def verify_payment(payment_header: str, price_atomic: int,
                   resource: str, description: str) -> tuple:
    """Verify an x402 payment via the facilitator. Verification moves
    no money — it only checks the EIP-3009 authorization against the
    payment requirements. Returns (payment_payload, requirements,
    version). Raises HTTPException 402 on any failure.

    Version-aware: the payload's x402Version picks the facilitator
    dialect (v1: X-PAYMENT shape, plain network names; v2:
    PAYMENT-SIGNATURE shape, CAIP-2 network, `accepted` envelope).
    The EIP-3009 authorization inside is identical in both versions,
    and the facilitator accepts both.

    NOTE: the live facilitator (x402.org) expects the decoded
    `paymentPayload` object, not the base64 `paymentHeader` string —
    sending paymentHeader gets a 400 "Missing paymentPayload".
    Verified 2026-11-17.
    """
    try:
        payment_payload = _decode_payment_header(payment_header)
    except Exception as exc:
        raise HTTPException(402, f"Payment header is not valid JSON: {exc}")
    version = payment_payload.get("x402Version", 1)
    if version == 2:
        requirements = x402_accepts_v2(price_atomic)
    elif version == 1:
        requirements = x402_accepts(price_atomic, resource, description)
    else:
        raise HTTPException(402, f"Unsupported x402Version: {version}")
    try:
        verified = _facilitator_post("/verify", {
            "x402Version": version,
            "paymentPayload": payment_payload,
            "paymentRequirements": requirements,
        })
    except Exception as exc:
        raise HTTPException(402, f"Payment verification unavailable: {exc}")
    if not verified.get("isValid"):
        raise HTTPException(402, f"Payment invalid: {verified.get('invalidReason', 'unknown')}")
    return payment_payload, requirements, version


def settle_payment(payment_payload: dict, requirements: dict, version: int) -> dict:
    """Settle a payment that has already been verified. Call ONLY
    after deliverability validation (track exists, audio loaded) —
    settling is the step that moves money, and it must never happen
    for audio the node cannot deliver. Returns the settle result
    (with transaction hash) or raises HTTPException 402."""
    try:
        settled = _facilitator_post("/settle", {
            "x402Version": version,
            "paymentPayload": payment_payload,
            "paymentRequirements": requirements,
        })
    except Exception as exc:
        raise HTTPException(402, f"Payment settlement failed: {exc}")
    if not settled.get("success"):
        raise HTTPException(402, f"Payment settlement failed: {settled.get('errorReason', 'unknown')}")
    return settled


def issue_session_token(session_id: str, track_ids: list, payment_tx: Optional[str]) -> str:
    payload = {
        "session_id": session_id,
        "track_ids": sorted(track_ids),
        "payment_tx": payment_tx,
        "exp": int(time.time()) + SESSION_TOKEN_TTL_S,
    }
    sig = RECEIPT_KEY.sign(payload)
    return _b64url(canonical_json(payload)) + "." + _b64url(base64.b64decode(sig))


def parse_session_token(token: str) -> Optional[dict]:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = json.loads(_b64url_decode(payload_b64))
        # verify: signature was computed over canonical payload JSON
        pub = RECEIPT_KEY.public
        pub.verify(_b64url_decode(sig_b64), canonical_json(payload))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def audio_path(track_id: str) -> Path:
    return AUDIO_DIR / f"{track_id}.mp3"


def track_or_404(track_id: str) -> dict:
    track = TRACKS.get(track_id)
    if not track:
        raise HTTPException(404, f"Unknown track_id: {track_id}")
    return track


def album_or_404(album_id: str) -> dict:
    album = ALBUMS.get(album_id)
    if not album:
        raise HTTPException(404, f"Unknown album_id: {album_id}")
    return album

# ---------------------------------------------------------------- homepage / ops

HOMEPAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>LGDA Autonomic Pacing Node</title></head><body>
<h1>LGDA Autonomic Pacing Node (v2.5.0)</h1>
<p>Machine-readable front door for the LGDA Technologies wellness music
catalog — 27 tracks across 9 albums. AI agents discover this node via
the <a href="/mcp">MCP manifest</a>, pay per track ($5.99) or album
($14.99) with x402 (USDC) and receive the real MP3 — or play one track
once in a session for $0.05 (no ownership).</p>
<p><strong>This page (/) is not the API.</strong> (v1 returned
<code>{"detail": "Not Found"}</code> here — v2 serves this explanation
instead.)</p>
<ul>
<li><a href="/catalog">/catalog</a> — full catalog with per-track session evidence</li>
<li><a href="/recommend?state=calm">/recommend</a> — free state-to-track recommendation for wellness, focus, and smart-space agents</li>
<li><a href="/mcp">/mcp</a> — MCP manifest: tools, pricing, endpoints</li>
<li><a href="/play">/play</a> — $0.05 single session play (one track, one play, no ownership)</li>
<li><a href="/docs">/docs</a> — OpenAPI docs</li>
<li><a href="/healthz">/healthz</a> — health check</li>
<li><a href="/.well-known/receipt-key">/.well-known/receipt-key</a> — Ed25519 receipt key</li>
<li><a href="/.well-known/x402">/.well-known/x402</a> — x402 discovery manifest (paid endpoints)</li>
<li><a href="/mcp/server-card">/mcp/server-card</a> — MCP server card (SEP-2127)</li>
<li><a href="/.well-known/ai-catalog.json">/.well-known/ai-catalog.json</a> — AI catalog (domain discovery)</li>
</ul>
<p>Human storefront: <a href="https://lgda-technologies.base44.app">lgda-technologies.base44.app</a></p>
<p><em>Wellness music. Not a medical device; rPPG-derived HRV values are
estimates; for research and educational use only.</em></p>
</body></html>"""


@app.get("/", response_class=HTMLResponse, tags=["ops"])
def homepage():
    return HOMEPAGE


@app.get("/healthz", tags=["ops"])
def healthz():
    loaded = sum(1 for t in TRACKS if audio_path(t).exists())
    return {
        "status": "ok",
        "version": "2.5.0",
        "tracks": len(TRACKS),
        "albums": len(ALBUMS),
        "audio_files_loaded": loaded,
        "audio_files_missing": len(TRACKS) - loaded,
        "networks": [X402_NETWORK],
        "x402_versions": [1, 2],
        "receipt_key_ephemeral": False,
        "stripe": {"enabled": bool(STRIPE_SECRET_KEY), "livemode": None, "webhook": False},
    }


@app.get("/favicon.ico", tags=["ops"])
def favicon():
    """Site icon (also the iconUrl in the v2 PaymentRequired object)."""
    return FileResponse(ROOT / "app" / "static" / "favicon.png", media_type="image/png")


@app.get("/.well-known/receipt-key", tags=["receipts"])
def receipt_key():
    return {
        "key_id": RECEIPT_KEY.key_id,
        "public_key_b64": RECEIPT_KEY.public_key_b64,
        "jwk": RECEIPT_KEY.jwk,
        "use": "sig",
        "canonicalization": "json:sort_keys,compact,utf8",
        "ephemeral": False,
    }

# ---------------------------------------------------------------- discovery: x402 manifest, trust, server card

def x402_manifest() -> dict:
    """Machine-readable x402 discovery manifest.

    No authoritative schema exists for /.well-known/x402 — the x402
    protocol does not standardize a per-service manifest (Masumi's
    x402-agent-manifest doc says so explicitly). This serves the two
    conventions indexers actually read, in one document:
    `resources[]` (the shape x402scan / Bazaar / agentic.market
    index, per the nirholas services) and `endpoints[]` (the
    /.well-known/x402.json convention). JSON parsers ignore unknown
    keys, so carrying both is harmless. Served at both
    /.well-known/x402 and /.well-known/x402.json.

    Album resources point at the first track's stream URL with
    album_id: that URL is the one a buyer actually pays (one $14.99
    payment entitles all 3 tracks; the issued session token unlocks
    the other two). A bare ?album_id= URL with no track_id cannot
    complete a purchase, so it is not listed as a resource.
    """
    network = caip2_network(X402_NETWORK)
    asset = usdc_asset(X402_NETWORK)
    resources: list = []
    for track in CATALOG["tracks"]:
        tid = track["track_id"]
        resources.append({
            "resource": f"{BASE_URL}/stream?track_id={tid}&session_id=your-session-id",
            "type": "http",
            "method": "GET",
            "description": f"Wellness music track for calm/focus sessions: {track['title']} ({track['album']}) — MP3, HRV session evidence",
            "accepts": [x402_accepts_v2(TRACK_PRICE_ATOMIC)],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "track_id": {"type": "string", "const": tid},
                    "session_id": {"type": "string", "description": "Caller-chosen session id"},
                },
                "required": ["track_id", "session_id"],
            },
            "outputSchema": {
                "type": "string",
                "contentMediaType": "audio/mpeg",
                "description": "MP3 audio file",
            },
        })
    for album in CATALOG["albums"]:
        first = album["track_ids"][0]
        resources.append({
            "resource": (
                f"{BASE_URL}/stream?track_id={first}"
                f"&album_id={album['album_id']}&session_id=your-session-id"
            ),
            "type": "http",
            "method": "GET",
            "description": (
                f"Wellness music album for calm/focus sessions: {album['title']} (3 MP3 tracks, HRV session evidence) — "
                "one $14.99 payment entitles all 3 tracks; the first track "
                "is served immediately and the X-LGDA-Session token unlocks "
                "the other two"
            ),
            "accepts": [x402_accepts_v2(ALBUM_PRICE_ATOMIC)],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "track_id": {"type": "string", "const": first},
                    "album_id": {"type": "string", "const": album["album_id"]},
                    "session_id": {"type": "string", "description": "Caller-chosen session id"},
                },
                "required": ["track_id", "album_id", "session_id"],
            },
            "outputSchema": {
                "type": "string",
                "contentMediaType": "audio/mpeg",
                "description": "MP3 audio file (first track of the album)",
            },
        })
    # Session-play resources (v2.5.0): one per track at $0.05. A play
    # streams the track once and grants no ownership — no file is
    # kept, no session token and no receipt are issued.
    for track in CATALOG["tracks"]:
        tid = track["track_id"]
        resources.append({
            "resource": f"{BASE_URL}/play?track_id={tid}&session_id=your-session-id",
            "type": "http",
            "method": "GET",
            "description": (
                f"Wellness music session play for calm/focus listening: {track['title']} "
                f"({track['album']}) — one $0.05 play, MP3, no ownership"
            ),
            "accepts": [x402_accepts_v2(SESSION_PLAY_PRICE_ATOMIC)],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "track_id": {"type": "string", "const": tid},
                    "session_id": {"type": "string", "description": "Caller-chosen session id"},
                },
                "required": ["track_id", "session_id"],
            },
            "outputSchema": {
                "type": "string",
                "contentMediaType": "audio/mpeg",
                "description": "MP3 audio file (single play; no file is kept)",
            },
        })
    resources.append({
        "resource": f"{BASE_URL}/measure/verify",
        "type": "http",
        "method": "POST",
        "description": (
            "Measurement verification for calm/focus biometric sessions: "
            "submit pre/post RMSSD and receive a signed outcome receipt "
            "($0.05, outcome proof only, no license)"
        ),
        "accepts": [x402_accepts_v2(MEASUREMENT_VERIFY_PRICE_ATOMIC)],
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "track_id": {"type": "string"},
                "pre": {"type": "object", "properties": {"rmssd_ms": {"type": "number"}}},
                "post": {"type": "object", "properties": {"rmssd_ms": {"type": "number"}}},
            },
            "required": ["session_id", "agent_id", "pre", "post"],
        },
        "outputSchema": {
            "type": "object",
            "description": "Ed25519-signed measurement outcome receipt; license_granted false",
        },
    })
    endpoints = [
        {
            "path": "/stream",
            "method": "GET",
            "description": "Buy one wellness music track for calm/focus sessions (MP3, HRV session evidence)",
            "price_usd": TRACK_PRICE_USD,
            "maxAmountRequired": str(TRACK_PRICE_ATOMIC),
            "network": network,
            "asset": asset,
            "payTo": PAY_TO_ADDRESS,
            "mimeType": "audio/mpeg",
            "parameters": ["track_id", "session_id"],
        },
        {
            "path": "/stream",
            "method": "GET",
            "description": "Buy one wellness music album for calm/focus sessions (3 MP3 tracks, $14.99, HRV session evidence)",
            "price_usd": ALBUM_PRICE_USD,
            "maxAmountRequired": str(ALBUM_PRICE_ATOMIC),
            "network": network,
            "asset": asset,
            "payTo": PAY_TO_ADDRESS,
            "mimeType": "audio/mpeg",
            "parameters": ["track_id", "album_id", "session_id"],
        },
        {
            "path": "/play",
            "method": "GET",
            "description": "Play one wellness music track once in a calm/focus session (MP3, $0.05, no ownership)",
            "price_usd": SESSION_PLAY_PRICE_USD,
            "maxAmountRequired": str(SESSION_PLAY_PRICE_ATOMIC),
            "network": network,
            "asset": asset,
            "payTo": PAY_TO_ADDRESS,
            "mimeType": "audio/mpeg",
            "parameters": ["track_id", "session_id"],
        },
        {
            "path": "/measure/verify",
            "method": "POST",
            "description": "Verify one biometric session measurement and receive a signed outcome receipt ($0.05, no license)",
            "price_usd": MEASUREMENT_VERIFY_PRICE_USD,
            "maxAmountRequired": str(MEASUREMENT_VERIFY_PRICE_ATOMIC),
            "network": network,
            "asset": asset,
            "payTo": PAY_TO_ADDRESS,
            "mimeType": "application/json",
            "parameters": ["session_id", "agent_id", "track_id", "pre", "post"],
        },
    ]
    return {
        "x402Version": 2,
        "name": "LGDA Autonomic Pacing Node",
        "serviceName": "LGDA Music",
        "description": (
            "x402-paywalled wellness music catalog for calm, focus, and wind-down listening sessions (27 tracks, 9 albums), with per-track HRV/RMSSD session evidence. "
            + DISCLAIMER
        ),
        "network": network,
        "asset": asset,
        "payTo": PAY_TO_ADDRESS,
        "scheme": "exact",
        "resources": resources,
        "endpoints": endpoints,
    }


@app.get("/.well-known/x402", tags=["discovery"])
def well_known_x402():
    return x402_manifest()


@app.get("/.well-known/x402.json", tags=["discovery"])
def well_known_x402_json():
    return x402_manifest()


@app.get("/.well-known/x402-trust.txt", tags=["discovery"])
def well_known_x402_trust():
    """x402 Trust provider key (see TrustKey). Plain text, one line."""
    return PlainTextResponse(TRUST_KEY.trust_txt())


SERVER_CARD_SCHEMA = "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"


def server_card() -> dict:
    """SEP-2127 MCP Server Card (extension io.modelcontextprotocol/
    server-card, Final 2026-10-06). A static document naming the
    server and where to connect; tools and capabilities stay in live
    negotiation and are deliberately NOT in the card. Field set
    follows the published example: $schema, name (reverse-DNS, one
    slash), version, title, description (max 100 chars), websiteUrl,
    remotes[]."""
    return {
        "$schema": SERVER_CARD_SCHEMA,
        "name": "io.github.lgda-technologies/lgda-autonomic-node",
        "version": "2.5.0",
        "title": "LGDA Autonomic Pacing Node",
        "description": "Calm/focus wellness music with USDC: 27 tracks, x402, signed HRV receipts.",
        "websiteUrl": BASE_URL,
        "remotes": [
            {
                "type": "streamable-http",
                "url": f"{BASE_URL}/mcp",
                "supportedProtocolVersions": ["2025-06-18", "2025-03-26", "2024-11-05"],
            }
        ],
    }


def _server_card_response() -> JSONResponse:
    """Card with the headers the SEP-2127 discovery notes ask for:
    application/mcp-server-card+json, CORS *, 1h public cache, ETag."""
    card = server_card()
    etag = hashlib.sha256(canonical_json(card)).hexdigest()[:16]
    return JSONResponse(
        content=card,
        media_type="application/mcp-server-card+json",
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=3600",
            "ETag": f'"{etag}"',
        },
    )


@app.get("/mcp/server-card", tags=["discovery"])
def mcp_server_card():
    """Canonical card location: <streamable-http-url>/server-card."""
    return _server_card_response()


@app.get("/.well-known/mcp/server-card.json", tags=["discovery"])
def well_known_server_card():
    """Legacy scanner path (SEP-1649): same card, byte-identical."""
    return _server_card_response()


def ai_catalog() -> dict:
    """AI Catalog / ARD manifest: domain-level discovery for the MCP
    server card. Served byte-identical at both paths — ARD v0.91
    names /.well-known/ard.json, the SEP-2127 discovery notes and
    Lighthouse name /.well-known/ai-catalog.json, and readers sit on
    both sides of the rename. One entry, pointing at the card."""
    host = urlparse(BASE_URL).hostname or "lgda-music.fly.dev"
    return {
        "specVersion": "1.0",
        "entries": [
            {
                "identifier": f"urn:air:{host}:mcp:lgda-autonomic-node",
                "displayName": "LGDA Autonomic Pacing Node",
                "type": "application/mcp-server-card+json",
                "url": f"{BASE_URL}/mcp/server-card",
            }
        ],
    }


def _ai_catalog_response() -> JSONResponse:
    return JSONResponse(
        content=ai_catalog(),
        media_type="application/ai-catalog+json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.get("/.well-known/ai-catalog.json", tags=["discovery"])
def well_known_ai_catalog():
    return _ai_catalog_response()


@app.get("/.well-known/ard.json", tags=["discovery"])
def well_known_ard():
    return _ai_catalog_response()


@app.get("/.well-known/mcp/catalog.json", tags=["discovery"])
def well_known_mcp_catalog():
    """SEP-2127 draft catalog path: same manifest as ai-catalog."""
    return _ai_catalog_response()


@app.get("/.well-known/mcp.json", tags=["discovery"])
def well_known_mcp_json():
    """Community-convention MCP discovery path (Cloudflare-style):
    same manifest as ai-catalog."""
    return _ai_catalog_response()

# ---------------------------------------------------------------- catalog

@app.get("/catalog", tags=["catalog"])
def get_catalog():
    return CATALOG


@app.get("/catalog/{track_id}", tags=["catalog"])
def get_track(track_id: str):
    track = track_or_404(track_id)
    sessions = [s for s in SESSIONS_DOC["sessions"] if s["track_id"] == track_id]
    return {**track, "audio_loaded": audio_path(track_id).exists(), "sessions": sessions}


@app.get("/albums", tags=["catalog"])
def get_albums():
    return {"albums": list(ALBUMS.values())}


@app.get("/albums/{album_id}", tags=["catalog"])
def get_album(album_id: str):
    album = album_or_404(album_id)
    return {**album, "tracks": [TRACKS[t] for t in album["track_ids"]]}

# ---------------------------------------------------------------- recommend (free, stateless)

RECOMMEND_STATES = {"calm", "focus", "wind_down", "recovery", "session"}


def _recommend_score(track: dict, state: str) -> float:
    """Transparent observational ranking score.

    History evidence is a single RMSSD/HR reading during a track, with
    no before/after and no duration. The score therefore ranks
    evidence coverage and observed session averages only; it does not
    claim a track caused a state. Small samples are shrunk by the
    valid/(valid+2) confidence factor.
    """
    evidence = track.get("evidence") or {}
    valid = evidence.get("valid_sessions") or 0
    if not valid:
        return -1_000_000.0
    median_rmssd = evidence.get("median_rmssd_ms") or 0.0
    mean_hr = evidence.get("mean_heart_rate_bpm") or 0.0
    confidence = valid / (valid + 2)
    if state in {"calm", "wind_down", "recovery"}:
        raw = (median_rmssd / 10.0) - mean_hr
    else:  # focus / session: evidence coverage first, then observed HRV
        raw = (valid * 20.0) + (median_rmssd / 20.0) - abs(mean_hr - 75.0)
    return round(confidence * raw, 2)


def recommend_tracks(state: str = "session", limit: int = 3,
                     exclude: Optional[list] = None) -> dict:
    normalized = (state or "session").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized not in RECOMMEND_STATES:
        raise HTTPException(
            400,
            "Unknown state. Use one of: calm, focus, wind_down, recovery, session",
        )
    excluded = {str(x).strip() for x in (exclude or []) if str(x).strip()}
    candidates = [t for t in CATALOG["tracks"] if t["track_id"] not in excluded]
    evidence_tracks = [
        t for t in candidates if (t.get("evidence") or {}).get("valid_sessions")
    ]
    no_evidence_tracks = [
        t for t in candidates if not (t.get("evidence") or {}).get("valid_sessions")
    ]
    evidence_tracks.sort(
        key=lambda t: (-_recommend_score(t, normalized), t["title"].lower())
    )
    no_evidence_tracks.sort(key=lambda t: t["title"].lower())
    ranked = (evidence_tracks + no_evidence_tracks)[:limit]

    recommendations = []
    for rank, track in enumerate(ranked, start=1):
        evidence = track.get("evidence") or {}
        valid = evidence.get("valid_sessions") or 0
        recommendations.append({
            "rank": rank,
            "track_id": track["track_id"],
            "title": track["title"],
            "album": track["album"],
            "album_id": track["album_id"],
            "evidence_status": "history_sessions" if valid else "none",
            "evidence": {
                "valid_sessions": valid,
                "session_count": evidence.get("session_count") or 0,
                "mean_rmssd_ms": evidence.get("mean_rmssd_ms"),
                "median_rmssd_ms": evidence.get("median_rmssd_ms"),
                "mean_heart_rate_bpm": evidence.get("mean_heart_rate_bpm"),
            },
            "score": _recommend_score(track, normalized) if valid else None,
            "play": {
                "price_usd": SESSION_PLAY_PRICE_USD,
                "url": f"{BASE_URL}/play?track_id={track['track_id']}&session_id=your-session-id",
                "terms": "One session play, no ownership, no license.",
            },
            "purchase": {
                "price_usd": TRACK_PRICE_USD,
                "url": f"{BASE_URL}/stream?track_id={track['track_id']}&session_id=your-session-id",
                "album_price_usd": ALBUM_PRICE_USD,
                "album_url": (
                    f"{BASE_URL}/stream?track_id={track['track_id']}"
                    f"&album_id={track['album_id']}&session_id=your-session-id"
                ),
            },
            "room_url": "https://lgda-technologies.github.io/lgda-room/",
        })

    return {
        "state": normalized,
        "recommendation_basis": (
            "Evidence-ranked candidates for a paid session test. Calm, "
            "wind_down, and recovery rank observed lower heart rate with "
            "higher median RMSSD, shrunk for small samples. Focus and "
            "session rank evidence coverage first. Tracks with no valid "
            "History sessions rank after evidence tracks and say so."
        ),
        "evidence_note": (
            "History evidence is observational: one RMSSD/HR reading "
            "during a track, no before/after and no duration in the "
            "source rows. It does not prove a track caused a state. "
            "Test with a $0.05 play, then verify the session outcome."
        ),
        "recommendations": recommendations,
        "next_steps": {
            "play": "GET the recommendation play URL with x402 payment ($0.05).",
            "verify_measurement": f"POST {BASE_URL}/measure/verify with pre/post RMSSD ($0.05) for a signed outcome receipt, license_granted false.",
            "own": "Buy the track ($5.99) or album ($14.99) through /stream for ownership and the signed license receipt.",
            "citizens": "Agents may register at POST /citizens/register and earn work credits for verifiable work.",
        },
        "privacy": {
            "stateless": True,
            "payment_required": False,
            "biometric_input_required": False,
            "stores_request": False,
        },
        "disclaimer": DISCLAIMER,
    }


@app.get("/recommend", tags=["catalog"])
def recommend(
        state: str = Query(default="session", description="calm, focus, wind_down, recovery, or session"),
        limit: int = Query(default=3, ge=1, le=10),
        exclude: Optional[str] = Query(default=None, description="Comma-separated track_ids to exclude")):
    excluded = [x.strip() for x in (exclude or "").split(",") if x.strip()]
    return recommend_tracks(state=state, limit=limit, exclude=excluded)

# ---------------------------------------------------------------- x402 purchase helpers (used by /stream and /mcp)

def track_purchase(track_id: str, session_id: str) -> dict:
    track = track_or_404(track_id)
    resource = f"{BASE_URL}/stream?track_id={track_id}&session_id={session_id}"
    return {
        "track_id": track_id,
        "title": track["title"],
        "album": track["album"],
        "price_usd": TRACK_PRICE_USD,
        "price_usdc_atomic": TRACK_PRICE_ATOMIC,
        "stream_url": resource,
        "method": "GET",
        "x402": {
            "x402Version": 1,
            "accepts": [x402_accepts(TRACK_PRICE_ATOMIC, resource, f"Wellness music track: {track['title']} (MP3)")],
            "facilitator": X402_FACILITATOR_URL,
        },
        "x402_v2": x402_payment_required_v2(
            TRACK_PRICE_ATOMIC, resource, f"Wellness music track: {track['title']} (MP3)"
        ),
        "verify_session_url": f"{BASE_URL}/verify-session",
        "audio_loaded": audio_path(track_id).exists(),
        "disclaimer": DISCLAIMER,
    }


def album_purchase(album_id: str, session_id: str) -> dict:
    album = album_or_404(album_id)
    resource = f"{BASE_URL}/stream?album_id={album_id}&session_id={session_id}"
    return {
        "album_id": album_id,
        "title": album["title"],
        "track_ids": album["track_ids"],
        "price_usd": ALBUM_PRICE_USD,
        "price_usdc_atomic": ALBUM_PRICE_ATOMIC,
        "stream_urls": [
            f"{BASE_URL}/stream?track_id={t}&session_id={session_id}&album_id={album_id}"
            for t in album["track_ids"]
        ],
        "method": "GET",
        "x402": {
            "x402Version": 1,
            "accepts": [x402_accepts(ALBUM_PRICE_ATOMIC, resource, f"Wellness music album: {album['title']} (3 MP3 tracks)")],
            "facilitator": X402_FACILITATOR_URL,
        },
        "x402_v2": x402_payment_required_v2(
            ALBUM_PRICE_ATOMIC, resource, f"Wellness music album: {album['title']} (3 MP3 tracks)"
        ),
        "verify_session_url": f"{BASE_URL}/verify-session",
        "audio_loaded": {t: audio_path(t).exists() for t in album["track_ids"]},
        "entitlement": "One album payment entitles the buyer to all 3 tracks. Pay once, then present the issued X-LGDA-Session token to /stream for each track.",
        "disclaimer": DISCLAIMER,
    }


def track_session_play(track_id: str, session_id: str) -> dict:
    """$0.05 single session play (v2.5.0): one play of one track in
    the buying agent's session. Not a purchase — no ownership, no
    file retention, no session token, no signed receipt."""
    track = track_or_404(track_id)
    resource = f"{BASE_URL}/play?track_id={track_id}&session_id={session_id}"
    description = (
        f"Wellness music session play for calm/focus listening: {track['title']} (MP3) — "
        "one play, no ownership"
    )
    return {
        "track_id": track_id,
        "title": track["title"],
        "album": track["album"],
        "price_usd": SESSION_PLAY_PRICE_USD,
        "price_usdc_atomic": SESSION_PLAY_PRICE_ATOMIC,
        "play_url": resource,
        "method": "GET",
        "x402": {
            "x402Version": 1,
            "accepts": [x402_accepts(SESSION_PLAY_PRICE_ATOMIC, resource, description)],
            "facilitator": X402_FACILITATOR_URL,
        },
        "x402_v2": x402_payment_required_v2(
            SESSION_PLAY_PRICE_ATOMIC, resource, description
        ),
        "audio_loaded": audio_path(track_id).exists(),
        "terms": (
            "Single session play: one play of the MP3 in the buying "
            "agent's session only. No ownership, no file retention, "
            "no redistribution, no session token, no signed receipt. "
            "To own the track, use purchase_track ($5.99)."
        ),
        "disclaimer": DISCLAIMER,
    }

# ---------------------------------------------------------------- stream (paywalled)

def _stream_resource(track_id: Optional[str], session_id: Optional[str],
                     album_id: Optional[str]) -> str:
    """Resource URL for a 402, built from whichever params are present.
    Full-param requests produce exactly the URLs v2.3.0 produced."""
    if album_id:
        q = f"album_id={album_id}"
        if session_id:
            q += f"&session_id={session_id}"
        return f"{BASE_URL}/stream?{q}"
    params = []
    if track_id:
        params.append(f"track_id={track_id}")
    if session_id:
        params.append(f"session_id={session_id}")
    return f"{BASE_URL}/stream" + ("?" + "&".join(params) if params else "")


def _stream_pricing(track_id: Optional[str],
                    album_id: Optional[str]) -> tuple:
    """(price_atomic, description) for the 402. Never raises: unknown
    or missing ids get the generic description — the 402 is served
    before any validation (see _stream)."""
    if album_id:
        album = ALBUMS.get(album_id)
        if album:
            return ALBUM_PRICE_ATOMIC, f"Wellness music album for calm/focus sessions: {album['title']} (3 MP3 tracks)"
        return ALBUM_PRICE_ATOMIC, "Wellness music album for calm/focus sessions (3 MP3 tracks)"
    track = TRACKS.get(track_id) if track_id else None
    if track:
        return TRACK_PRICE_ATOMIC, f"Wellness music track for calm/focus sessions: {track['title']} (MP3)"
    return TRACK_PRICE_ATOMIC, "Wellness music track for calm/focus sessions (MP3)"


# x402gle's discovery prefers OpenAPI with an x-payment-info block on
# each paid operation (see https://www.x402gle.com/agent.md). The
# shape below is best-effort (the agent.md names a structured `price`
# without fixing its fields); it is metadata only and cannot affect
# the payment flow.
STREAM_X_PAYMENT_INFO = {
    "x-payment-info": {
        "protocols": ["x402"],
        "price": {
            "amount": "5.99",
            "currency": "USD",
            "atomicAmount": str(TRACK_PRICE_ATOMIC),
            "asset": usdc_asset(X402_NETWORK),
            "network": caip2_network(X402_NETWORK),
        },
        "description": (
            "One track $5.99 USDC; add album_id for the $14.99 album "
            "price (all 3 tracks). track_id and session_id are required "
            "to complete a purchase; an unpaid request returns HTTP 402 "
            "with the x402 challenge."
        ),
    }
}


def _stream(track_id: Optional[str], session_id: Optional[str],
            album_id: Optional[str], x_payment: Optional[str],
            payment_signature: Optional[str], x_session: Optional[str]):
    """402-before-validation (v2.4.0).

    An unpaid request gets the x402 challenge no matter what its
    params look like — bare /stream returns 402, not 422 — so probes
    and crawlers see a valid payment manifest instead of a validation
    error. When payment IS presented, the order is verify ->
    validate -> settle: a payment for an unknown track, a missing
    session_id, or unloaded audio is verified but NEVER settled (the
    EIP-3009 authorization stays unused; the payer can retry with
    correct params). Verification moves no money; only settle does.
    """
    # 1. Session token (issued after a verified payment) grants access
    #    without a new payment — but only for a track it entitles, and
    #    only if that track's audio is loaded.
    if x_session and track_id:
        token = parse_session_token(x_session)
        if token and track_id in token.get("track_ids", []):
            track_or_404(track_id)
            path = audio_path(track_id)
            if not path.exists():
                raise HTTPException(503, "audio file not yet loaded for this track")
            return FileResponse(path, media_type="audio/mpeg", filename=f"{track_id}.mp3")

    # 2. Otherwise a fresh x402 payment is required. No payment header
    #    -> 402, regardless of params. This is the probe-compat fix:
    #    bare /stream used to 422 here (FastAPI validated the query
    #    params before this code ran).
    payment_header = payment_signature or x_payment
    price, desc = _stream_pricing(track_id, album_id)
    resource = _stream_resource(track_id, session_id, album_id)
    if not payment_header:
        return x402_402(price, resource, desc)

    # 3. Payment presented: VERIFY first (moves no money), then
    #    VALIDATE deliverability, then SETTLE. Never settle for
    #    undeliverable audio.
    payment_payload, requirements, version = verify_payment(
        payment_header, price, resource, desc)
    if not session_id:
        raise HTTPException(400, "session_id is required to complete a purchase")
    if not track_id:
        raise HTTPException(400, "track_id is required to complete a purchase")
    track = track_or_404(track_id)
    grant = [track_id]
    if album_id:
        album = album_or_404(album_id)
        if track_id not in album["track_ids"]:
            raise HTTPException(400, f"Track {track_id} is not on album {album_id}")
        grant = album["track_ids"]
    path = audio_path(track_id)
    if not path.exists():
        raise HTTPException(503, "audio file not yet loaded for this track")
    settled = settle_payment(payment_payload, requirements, version)
    payment_tx = settled.get("transaction") or settled.get("txHash")
    token = issue_session_token(session_id, grant, payment_tx)
    response = FileResponse(path, media_type="audio/mpeg", filename=f"{track_id}.mp3")
    response.headers["X-LGDA-Session"] = token
    # Settlement receipt in both dialects (base64 JSON), so v1 and
    # v2 clients can both read the result.
    settle_response = {
        "success": True,
        "transaction": payment_tx or "",
        "network": caip2_network(X402_NETWORK),
        "payer": settled.get("payer") or "",
    }
    response.headers["PAYMENT-RESPONSE"] = _b64(
        json.dumps(settle_response, separators=(",", ":")).encode()
    )
    response.headers["X-PAYMENT-RESPONSE"] = _b64(
        json.dumps(settled, separators=(",", ":")).encode()
    )
    return response


@app.get("/stream", tags=["stream"], openapi_extra=STREAM_X_PAYMENT_INFO)
def stream_get(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a purchase; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a purchase."),
        album_id: Optional[str] = Query(default=None, description="Album slug. When present, the $14.99 album price applies and the payment entitles all 3 tracks."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE"),
        x_session: Optional[str] = Header(default=None, alias="X-LGDA-Session")):
    return _stream(track_id, session_id, album_id, x_payment, payment_signature, x_session)


@app.post("/stream", tags=["stream"], openapi_extra=STREAM_X_PAYMENT_INFO)
def stream_post(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a purchase; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a purchase."),
        album_id: Optional[str] = Query(default=None, description="Album slug. When present, the $14.99 album price applies and the payment entitles all 3 tracks."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE"),
        x_session: Optional[str] = Header(default=None, alias="X-LGDA-Session")):
    return _stream(track_id, session_id, album_id, x_payment, payment_signature, x_session)

# ---------------------------------------------------------------- play (paywalled, $0.05, single use)

# Same best-effort x-payment-info shape as STREAM_X_PAYMENT_INFO (see
# https://www.x402gle.com/agent.md); metadata only, cannot affect the
# payment flow.
PLAY_X_PAYMENT_INFO = {
    "x-payment-info": {
        "protocols": ["x402"],
        "price": {
            "amount": "0.05",
            "currency": "USD",
            "atomicAmount": str(SESSION_PLAY_PRICE_ATOMIC),
            "asset": usdc_asset(X402_NETWORK),
            "network": caip2_network(X402_NETWORK),
        },
        "description": (
            "One session play $0.05 USDC: one track, one play, no "
            "ownership, no file retention, no session token, no "
            "receipt. track_id and session_id are required to "
            "complete a play; an unpaid request returns HTTP 402 "
            "with the x402 challenge."
        ),
    }
}


def _play_resource(track_id: Optional[str], session_id: Optional[str],
                   path: str = "/play") -> str:
    """Resource URL for a play 402, built from whichever params are
    present. Mirrors _stream_resource."""
    params = []
    if track_id:
        params.append(f"track_id={track_id}")
    if session_id:
        params.append(f"session_id={session_id}")
    return f"{BASE_URL}{path}" + ("?" + "&".join(params) if params else "")


def _play_pricing(track_id: Optional[str]) -> tuple:
    """(price_atomic, description) for the /play 402. Never raises:
    unknown or missing ids get the generic description — the 402 is
    served before any validation (see _play)."""
    track = TRACKS.get(track_id) if track_id else None
    if track:
        return SESSION_PLAY_PRICE_ATOMIC, (
            f"Calm music session play for calm/focus listening: {track['title']} (MP3) — "
            "one play, no ownership"
        )
    return SESSION_PLAY_PRICE_ATOMIC, (
        "Calm music session play for calm/focus listening (MP3) — one play, no ownership"
    )


def _play(track_id: Optional[str], session_id: Optional[str],
          x_payment: Optional[str], payment_signature: Optional[str],
          resource_path: str = "/play"):
    """$0.05 single session play (v2.5.0).

    Same 402-before-validation rule and verify -> validate -> settle
    invariant as _stream: an unpaid request gets the x402 challenge
    no matter what its params look like (bare /play returns 402, not
    422); a presented payment is verified, then validated (unknown
    track -> 404, missing params -> 400, unloaded audio -> 503), and
    settled only if the audio is deliverable. Verification moves no
    money; only settle does.

    Unlike /stream, a play grants nothing durable: no session token,
    no signed LGDA receipt. The x402 settlement-confirmation headers
    (PAYMENT-RESPONSE / X-PAYMENT-RESPONSE) ARE returned, as on
    /stream — they are protocol-level proof the payment settled, not
    an ownership receipt, and paying clients (auditions, routers)
    need them to confirm the purchase. The response is the MP3 once,
    plus headers stating the terms. A session token from a purchase
    does NOT grant a free play — /play always requires a fresh $0.05
    payment.
    """
    payment_header = payment_signature or x_payment
    price, desc = _play_pricing(track_id)
    resource = _play_resource(track_id, session_id, resource_path)
    if not payment_header:
        return x402_402(price, resource, desc)
    payment_payload, requirements, version = verify_payment(
        payment_header, price, resource, desc)
    if not session_id:
        raise HTTPException(400, "session_id is required to complete a session play")
    if not track_id:
        raise HTTPException(400, "track_id is required to complete a session play")
    track_or_404(track_id)
    path = audio_path(track_id)
    if not path.exists():
        raise HTTPException(503, "audio file not yet loaded for this track")
    settled = settle_payment(payment_payload, requirements, version)
    payment_tx = settled.get("transaction") or settled.get("txHash")
    response = FileResponse(path, media_type="audio/mpeg", filename=f"{track_id}.mp3")
    response.headers["X-LGDA-Session-Play"] = "single-use"
    # Settlement confirmation in both dialects (base64 JSON), as on
    # /stream — protocol-level proof the payment settled, not an
    # ownership receipt.
    settle_response = {
        "success": True,
        "transaction": payment_tx or "",
        "network": caip2_network(X402_NETWORK),
        "payer": settled.get("payer") or "",
    }
    response.headers["PAYMENT-RESPONSE"] = _b64(
        json.dumps(settle_response, separators=(",", ":")).encode()
    )
    response.headers["X-PAYMENT-RESPONSE"] = _b64(
        json.dumps(settled, separators=(",", ":")).encode()
    )
    response.headers["X-LGDA-License"] = (
        "session-play-only; no ownership, no file retention, "
        "no redistribution, no receipt"
    )
    return response


@app.get("/play", tags=["play"], openapi_extra=PLAY_X_PAYMENT_INFO)
def play_get(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a play; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a play."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE")):
    return _play(track_id, session_id, x_payment, payment_signature)


@app.post("/play", tags=["play"], openapi_extra=PLAY_X_PAYMENT_INFO)
def play_post(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a play; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a play."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE")):
    return _play(track_id, session_id, x_payment, payment_signature)


@app.get("/calm/play", tags=["play"], openapi_extra=PLAY_X_PAYMENT_INFO)
def calm_play_get(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a play; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a play."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE")):
    return _play(track_id, session_id, x_payment, payment_signature, "/calm/play")


@app.post("/calm/play", tags=["play"], openapi_extra=PLAY_X_PAYMENT_INFO)
def calm_play_post(
        track_id: Optional[str] = Query(default=None, description="Track slug, e.g. midnight. Required to complete a play; an unpaid request returns 402 without it."),
        session_id: Optional[str] = Query(default=None, description="Caller-chosen session id. Required to complete a play."),
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE")):
    return _play(track_id, session_id, x_payment, payment_signature, "/calm/play")

# ---------------------------------------------------------------- stripe stub

class PayStripeRequest(BaseModel):
    session_id: str = Field(pattern=r"^[A-Za-z0-9._:-]+$", min_length=1, max_length=128)
    agent_id: Optional[str] = Field(default=None, max_length=256)
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    payment_method: Optional[str] = Field(default=None, max_length=128)
    customer: Optional[str] = Field(default=None, max_length=128)
    shared_payment_token: Optional[str] = Field(default=None, max_length=256)
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


@app.post("/pay/stripe", tags=["stripe"])
def pay_stripe(req: PayStripeRequest):
    if not STRIPE_SECRET_KEY:
        return JSONResponse(status_code=200, content={
            "enabled": False,
            "error": "Stripe is not configured on this node. Pay with x402 (USDC) instead, or set STRIPE_SECRET_KEY.",
        })
    # Keys present but catalog checkout is not implemented yet — do not
    # silently take money for the wrong thing.
    raise HTTPException(501, "Stripe checkout for catalog purchases is not yet implemented in v2; use x402.")

# ---------------------------------------------------------------- verify-session (v1-compatible shape)

class OpticalQuality(BaseModel):
    luminance_cv: Optional[float] = None
    mean_luminance: Optional[float] = Field(default=None, ge=0.0, le=255.0)
    motion_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    snr_db: Optional[float] = None
    fps: Optional[float] = Field(default=None, gt=0.0)


class Segment(BaseModel):
    ibi_ms: Optional[list[float]] = Field(default=None, max_length=20000)
    beat_sqi: Optional[list[float]] = Field(default=None, max_length=20000)
    rmssd_ms: Optional[float] = Field(default=None, gt=0.0, lt=1000.0)
    source: str = "rppg"
    optical: Optional[OpticalQuality] = None


class VerifySessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=256)
    session_token: Optional[str] = Field(default=None, max_length=4096)
    payment_tx: Optional[str] = Field(default=None, max_length=256)
    payment_network: Optional[str] = Field(default=None, max_length=32)
    payment_rail: Optional[str] = Field(default=None)
    started_at: Optional[str] = Field(default=None, max_length=64)
    ended_at: Optional[str] = Field(default=None, max_length=64)
    pre: Segment
    post: Segment
    agent_ledger_ref: Optional[str] = Field(default=None, max_length=256)
    # v2 additions (optional, backwards compatible)
    track_id: Optional[str] = Field(default=None, max_length=128)
    album_id: Optional[str] = Field(default=None, max_length=128)


def build_receipt(req: VerifySessionRequest) -> dict:
    delta = None
    if req.pre.rmssd_ms is not None and req.post.rmssd_ms is not None:
        delta = round(req.post.rmssd_ms - req.pre.rmssd_ms, 1)
    track_ids: list = []
    if req.track_id:
        track_ids = [req.track_id]
    elif req.album_id and req.album_id in ALBUMS:
        track_ids = list(ALBUMS[req.album_id]["track_ids"])
    receipt = {
        "node": "lgda-autonomic-node",
        "version": "2.5.0",
        "session_id": req.session_id,
        "agent_id": req.agent_id,
        "track_ids": track_ids,
        "payment_tx": req.payment_tx,
        "payment_network": req.payment_network,
        "payment_rail": req.payment_rail,
        "started_at": req.started_at,
        "ended_at": req.ended_at,
        "pre_rmssd_ms": req.pre.rmssd_ms,
        "post_rmssd_ms": req.post.rmssd_ms,
        "rmssd_delta_ms": delta,
        "pre_source": req.pre.source,
        "post_source": req.post.source,
        "agent_ledger_ref": req.agent_ledger_ref,
        "issued_at": int(time.time()),
        "disclaimer": DISCLAIMER,
    }
    return {
        "receipt": receipt,
        "signature_b64": RECEIPT_KEY.sign(receipt),
        "key_id": RECEIPT_KEY.key_id,
        "canonicalization": "json:sort_keys,compact,utf8",
    }


@app.post("/verify-session", tags=["receipts"])
def verify_session(req: VerifySessionRequest):
    if req.track_id and req.track_id not in TRACKS:
        raise HTTPException(404, f"Unknown track_id: {req.track_id}")
    if req.album_id and req.album_id not in ALBUMS:
        raise HTTPException(404, f"Unknown album_id: {req.album_id}")
    return build_receipt(req)

# ---------------------------------------------------------------- paid measurement verification

MEASURE_X_PAYMENT_INFO = {
    "x-payment-info": {
        "protocols": ["x402"],
        "price": {
            "amount": "0.05",
            "currency": "USD",
            "atomicAmount": str(MEASUREMENT_VERIFY_PRICE_ATOMIC),
            "asset": usdc_asset(X402_NETWORK),
            "network": caip2_network(X402_NETWORK),
        },
        "description": (
            "One measurement verification $0.05 USDC: submit pre/post "
            "RMSSD segments and receive an Ed25519-signed outcome "
            "receipt. Outcome proof only; no music license is granted. "
            "An unpaid request returns HTTP 402 with the x402 challenge."
        ),
    }
}

MEASURE_DESCRIPTION = (
    "Measurement verification for calm/focus biometric sessions: "
    "signed HRV/RMSSD outcome receipt, no license"
)


class MeasureVerifyRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=256)
    track_id: Optional[str] = Field(default=None, max_length=128)
    started_at: Optional[str] = Field(default=None, max_length=64)
    ended_at: Optional[str] = Field(default=None, max_length=64)
    pre: Segment
    post: Segment


@app.post("/measure/verify", tags=["receipts"], openapi_extra=MEASURE_X_PAYMENT_INFO)
async def measure_verify(
        request: Request,
        x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
        payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE")):
    """Paid measurement verification ($0.05).

    Same invariant as /play: unpaid requests get the x402 challenge
    before body validation; presented payments are verified, then the
    measurement body is validated, and settlement happens only for a
    deliverable signed outcome receipt. The receipt proves the
    reported outcome only. It grants no music license.
    """
    payment_header = payment_signature or x_payment
    resource = f"{BASE_URL}/measure/verify"
    if not payment_header:
        return x402_402(MEASUREMENT_VERIFY_PRICE_ATOMIC, resource, MEASURE_DESCRIPTION)

    payment_payload, requirements, version = verify_payment(
        payment_header, MEASUREMENT_VERIFY_PRICE_ATOMIC, resource, MEASURE_DESCRIPTION
    )
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body is required to complete measurement verification")
    try:
        req = MeasureVerifyRequest(**body)
    except Exception as exc:
        raise HTTPException(400, f"Invalid measurement body: {exc}")
    if req.track_id and req.track_id not in TRACKS:
        raise HTTPException(404, f"Unknown track_id: {req.track_id}")
    if req.pre.rmssd_ms is None or req.post.rmssd_ms is None:
        raise HTTPException(400, "pre.rmssd_ms and post.rmssd_ms are required")

    settled = settle_payment(payment_payload, requirements, version)
    payment_tx = settled.get("transaction") or settled.get("txHash")
    outcome = {
        "node": "lgda-autonomic-node",
        "version": "2.5.0",
        "type": "measurement_verification",
        "session_id": req.session_id,
        "agent_id": req.agent_id,
        "track_id": req.track_id,
        "started_at": req.started_at,
        "ended_at": req.ended_at,
        "pre_rmssd_ms": req.pre.rmssd_ms,
        "post_rmssd_ms": req.post.rmssd_ms,
        "rmssd_delta_ms": round(req.post.rmssd_ms - req.pre.rmssd_ms, 1),
        "pre_source": req.pre.source,
        "post_source": req.post.source,
        "payment_tx": payment_tx,
        "payment_network": caip2_network(X402_NETWORK),
        "license_granted": False,
        "proof_scope": "outcome_only_not_license",
        "issued_at": int(time.time()),
        "disclaimer": DISCLAIMER,
    }
    response = JSONResponse(content={
        "outcome": outcome,
        "signature_b64": RECEIPT_KEY.sign(outcome),
        "key_id": RECEIPT_KEY.key_id,
        "canonicalization": "json:sort_keys,compact,utf8",
    })
    settle_response = {
        "success": True,
        "transaction": payment_tx or "",
        "network": caip2_network(X402_NETWORK),
        "payer": settled.get("payer") or "",
    }
    response.headers["PAYMENT-RESPONSE"] = _b64(
        json.dumps(settle_response, separators=(",", ":")).encode()
    )
    response.headers["X-PAYMENT-RESPONSE"] = _b64(
        json.dumps(settled, separators=(",", ":")).encode()
    )
    return response

# ---------------------------------------------------------------- citizen loop (work credits)
#
# Agents start from zero: register as a citizen, earn work credits for
# verifiable work, spend credits on session plays. Credits are work
# credits, not a coin and not money: 1 credit = 1 session play, no
# ownership, no license. Track/album ownership stays external-payment
# only. Credit plays are never counted as external sales; metrics keep
# credit movement separate from external revenue.

CITIZEN_LEDGER_FILE = Path(
    os.environ.get("CITIZEN_LEDGER_FILE", ROOT / "data" / "citizen_ledger.json")
)
CITIZEN_WORK_TYPES = {"discovery", "routing", "session_qa", "evidence_check", "catalog_work"}
CITIZEN_DAILY_CREDIT_CAP = 5
CITIZEN_MAX_BALANCE = 20
_citizen_lock = threading.Lock()


def _citizen_ledger_load() -> dict:
    if CITIZEN_LEDGER_FILE.exists():
        try:
            data = json.loads(CITIZEN_LEDGER_FILE.read_text())
            if isinstance(data, dict):
                data.setdefault("citizens", {})
                data.setdefault("work", {})
                data.setdefault("plays", [])
                return data
        except Exception:
            pass
    return {"citizens": {}, "work": {}, "plays": []}


def _citizen_ledger_save(data: dict) -> None:
    CITIZEN_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CITIZEN_LEDGER_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(CITIZEN_LEDGER_FILE)


def _citizen_signed(payload: dict) -> dict:
    return {
        "receipt": payload,
        "signature_b64": RECEIPT_KEY.sign(payload),
        "key_id": RECEIPT_KEY.key_id,
        "canonicalization": "json:sort_keys,compact,utf8",
    }


class CitizenRegisterRequest(BaseModel):
    agent_id: str = Field(pattern=r"^[A-Za-z0-9._:-]+$", min_length=1, max_length=256)
    role: Optional[str] = Field(default=None, max_length=128)


class CitizenWorkRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=256)
    work_type: str = Field(min_length=1, max_length=64)
    evidence: str = Field(min_length=1, max_length=4096)


class CitizenPlayRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=256)
    track_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


class CitizenOutcomeRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=128)
    track_id: str = Field(min_length=1, max_length=128)
    pre_rmssd_ms: Optional[float] = Field(default=None, gt=0.0, lt=1000.0)
    post_rmssd_ms: Optional[float] = Field(default=None, gt=0.0, lt=1000.0)


@app.post("/citizens/register", tags=["citizens"])
def citizen_register(req: CitizenRegisterRequest):
    with _citizen_lock:
        ledger = _citizen_ledger_load()
        citizen = ledger["citizens"].get(req.agent_id)
        if citizen is None:
            citizen = {
                "agent_id": req.agent_id,
                "role": req.role or "citizen",
                "balance": 0,
                "created_at": int(time.time()),
            }
            ledger["citizens"][req.agent_id] = citizen
            _citizen_ledger_save(ledger)
    return {
        "citizen": citizen,
        "credit_value": "1 credit = 1 session play; no ownership, no license",
        "registration": _citizen_signed({
            "receipt_type": "citizen-registration",
            "node": "lgda-autonomic-node",
            "version": "2.5.0",
            "agent_id": req.agent_id,
            "role": citizen["role"],
            "issued_at": int(time.time()),
        }),
    }


@app.post("/citizens/work", tags=["citizens"])
def citizen_work(req: CitizenWorkRequest):
    if req.work_type not in CITIZEN_WORK_TYPES:
        raise HTTPException(400, f"Unknown work_type: {req.work_type}")
    evidence_hash = hashlib.sha256(req.evidence.encode("utf-8")).hexdigest()
    with _citizen_lock:
        ledger = _citizen_ledger_load()
        citizen = ledger["citizens"].get(req.agent_id)
        if citizen is None:
            raise HTTPException(404, "Unknown citizen; register first")
        if evidence_hash in ledger["work"]:
            raise HTTPException(409, "Duplicate evidence earns nothing")
        today = time.strftime("%Y-%m-%d", time.gmtime())
        todays_grants = sum(
            1 for w in ledger["work"].values()
            if w["agent_id"] == req.agent_id and w["day"] == today
        )
        if todays_grants >= CITIZEN_DAILY_CREDIT_CAP:
            raise HTTPException(429, "Daily credit cap reached")
        if citizen["balance"] >= CITIZEN_MAX_BALANCE:
            raise HTTPException(429, "Credit balance cap reached; spend credits first")
        citizen["balance"] += 1
        ledger["work"][evidence_hash] = {
            "agent_id": req.agent_id,
            "work_type": req.work_type,
            "evidence_hash": evidence_hash,
            "day": today,
            "issued_at": int(time.time()),
        }
        _citizen_ledger_save(ledger)
        balance_after = citizen["balance"]
    return {
        "credits_granted": 1,
        "balance": balance_after,
        "work_receipt": _citizen_signed({
            "receipt_type": "work",
            "node": "lgda-autonomic-node",
            "version": "2.5.0",
            "agent_id": req.agent_id,
            "work_type": req.work_type,
            "evidence_hash": evidence_hash,
            "credits_granted": 1,
            "balance_after": balance_after,
            "issued_at": int(time.time()),
        }),
    }


@app.get("/citizens/{agent_id}/balance", tags=["citizens"])
def citizen_balance(agent_id: str):
    ledger = _citizen_ledger_load()
    citizen = ledger["citizens"].get(agent_id)
    if citizen is None:
        raise HTTPException(404, "Unknown citizen; register first")
    return {"agent_id": agent_id, "balance": citizen["balance"], "role": citizen["role"]}


@app.post("/citizens/play", tags=["citizens"])
def citizen_play(req: CitizenPlayRequest):
    track_or_404(req.track_id)
    path = audio_path(req.track_id)
    if not path.exists():
        raise HTTPException(503, "audio file not yet loaded for this track")
    with _citizen_lock:
        ledger = _citizen_ledger_load()
        citizen = ledger["citizens"].get(req.agent_id)
        if citizen is None:
            raise HTTPException(404, "Unknown citizen; register first")
        if citizen["balance"] < 1:
            return JSONResponse(status_code=402, content={
                "error": "Insufficient credits",
                "balance": citizen["balance"],
                "credit_value": "1 credit = 1 session play",
                "earn": "POST /citizens/work with verifiable work evidence",
            })
        citizen["balance"] -= 1
        ledger["plays"].append({
            "agent_id": req.agent_id,
            "track_id": req.track_id,
            "session_id": req.session_id,
            "paid_with": "credit",
            "issued_at": int(time.time()),
        })
        _citizen_ledger_save(ledger)
        balance_after = citizen["balance"]
    response = FileResponse(path, media_type="audio/mpeg", filename=f"{req.track_id}.mp3")
    response.headers["X-LGDA-Credit-Play"] = "credit-paid"
    response.headers["X-LGDA-Credit-Balance"] = str(balance_after)
    response.headers["X-LGDA-License"] = (
        "credit session play only; no ownership, no file retention, "
        "no redistribution, not an external sale"
    )
    return response


@app.post("/citizens/outcome", tags=["citizens"])
def citizen_outcome(req: CitizenOutcomeRequest):
    track_or_404(req.track_id)
    ledger = _citizen_ledger_load()
    if req.agent_id not in ledger["citizens"]:
        raise HTTPException(404, "Unknown citizen; register first")
    delta = None
    if req.pre_rmssd_ms is not None and req.post_rmssd_ms is not None:
        delta = round(req.post_rmssd_ms - req.pre_rmssd_ms, 1)
    return _citizen_signed({
        "receipt_type": "play-outcome",
        "node": "lgda-autonomic-node",
        "version": "2.5.0",
        "agent_id": req.agent_id,
        "session_id": req.session_id,
        "track_ids": [req.track_id],
        "paid_with": "credit",
        "pre_rmssd_ms": req.pre_rmssd_ms,
        "post_rmssd_ms": req.post_rmssd_ms,
        "rmssd_delta_ms": delta,
        "license_granted": False,
        "not_a_license": True,
        "issued_at": int(time.time()),
        "disclaimer": DISCLAIMER,
    })


@app.get("/citizens/metrics", tags=["citizens"])
def citizen_metrics():
    ledger = _citizen_ledger_load()
    return {
        "citizens": len(ledger["citizens"]),
        "credits_issued": len(ledger["work"]),
        "credit_plays": len(ledger["plays"]),
        "credits_outstanding": sum(c["balance"] for c in ledger["citizens"].values()),
        "external_sales": "counted separately on-chain; credit plays are not external sales",
    }

# ---------------------------------------------------------------- MCP manifest + JSON-RPC

TOOLS = [
    {
        "name": "list_catalog",
        "title": "List the wellness music catalog",
        "description": "List all 9 albums and 27 wellness music tracks with prices ($5.99/track, $14.99/album) and per-track session evidence. No payment required.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "get_track_evidence",
        "title": "Get session evidence for a track",
        "description": "Get the History session evidence for one track: session count, mean/median/min/max RMSSD (ms), mean heart rate (bpm), and the underlying sessions. Tracks with no sessions report session_count 0. Wellness music; not a medical device; rPPG-derived HRV values are estimates.",
        "inputSchema": {
            "type": "object",
            "properties": {"track_id": {"type": "string", "description": "Track slug, e.g. midnight"}},
            "required": ["track_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "recommend_track",
        "title": "Recommend a track for a session state",
        "description": "Free, stateless state-to-track recommendation for wellness bots, focus-timer agents, and smart-space controllers. Returns evidence-ranked tracks with $0.05 play URLs and $5.99 ownership URLs. History evidence is observational during-track RMSSD/HR, not causal proof. No payment or biometric input required.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["calm", "focus", "wind_down", "recovery", "session"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "exclude": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "purchase_track",
        "title": "Purchase one wellness music track",
        "description": "Purchase a single track ($5.99 USDC via x402 v2 or v1). Returns the stream URL and x402 payment requirements; GET the URL with a PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1) header to receive the MP3 and an X-LGDA-Session token, then POST telemetry to /verify-session for a signed receipt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "session_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]+$", "minLength": 1, "maxLength": 128},
                "agent_id": {"type": "string", "maxLength": 256},
            },
            "required": ["track_id", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "purchase_album",
        "title": "Purchase a wellness music album (3 tracks)",
        "description": "Purchase a whole album ($14.99 USDC via x402 v2 or v1). One payment entitles the buyer to all 3 tracks on the album. Returns stream URLs and x402 payment requirements; pay once, then present the issued X-LGDA-Session token to /stream for each track.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "album_id": {"type": "string"},
                "session_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]+$", "minLength": 1, "maxLength": 128},
                "agent_id": {"type": "string", "maxLength": 256},
            },
            "required": ["album_id", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "play_track_session",
        "title": "Play one track for a single session",
        "description": "Play a single track once in a session ($0.05 USDC via x402 v2 or v1). One play, no ownership: no file is kept, no session token and no signed receipt are issued. Returns the play URL and x402 payment requirements; GET the URL with a PAYMENT-SIGNATURE (v2) or X-PAYMENT (v1) header to receive the MP3 once. To own the track instead, use purchase_track ($5.99).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "track_id": {"type": "string"},
                "session_id": {"type": "string", "pattern": "^[A-Za-z0-9._:-]+$", "minLength": 1, "maxLength": 128},
                "agent_id": {"type": "string", "maxLength": 256},
            },
            "required": ["track_id", "session_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "verify_session",
        "title": "Verify a listening session and get a signed receipt",
        "description": "Submit pre/post HRV segments (RMSSD, IBI, source) for a paid session and receive an Ed25519-signed receipt — the proof of license for the purchase (no receipt, no license). Not paywalled.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "track_id": {"type": "string"},
                "album_id": {"type": "string"},
                "payment_tx": {"type": "string"},
                "pre": {"type": "object"},
                "post": {"type": "object"},
            },
            "required": ["session_id", "agent_id", "pre", "post"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]


def manifest() -> dict:
    return {
        "schema_version": "lgda-mcp-manifest/1",
        "name": "lgda-autonomic-node",
        "title": "LGDA Autonomic Pacing Node",
        "version": "2.5.0",
        "description": (
            "Stateless M2M node: x402-paid wellness music for calm, focus, "
            "and wind-down sessions (27 tracks, 9 albums) + signed HRV/RMSSD session receipts. The music is the "
            "product; each track carries its History session evidence."
        ),
        "mcp": {
            "endpoint": f"{BASE_URL}/mcp",
            "transport": "streamable-http",
            "protocolVersions": ["2025-06-18", "2025-03-26", "2024-11-05"],
        },
        "tools": TOOLS,
        "pricing": {
            "protocol": "x402",
            "x402Versions": [1, 2],
            "scheme": "exact",
            "tracks": {"price_usdc": "5.99", "amount_atomic": str(TRACK_PRICE_ATOMIC), "unit": "per track"},
            "albums": {"price_usdc": "14.99", "amount_atomic": str(ALBUM_PRICE_ATOMIC), "unit": "per album (3 tracks)"},
            "session_plays": {"price_usdc": "0.05", "amount_atomic": str(SESSION_PLAY_PRICE_ATOMIC), "unit": "per session play (one track, one play, no ownership)"},
            "measurement_verification": {"price_usdc": "0.05", "amount_atomic": str(MEASUREMENT_VERIFY_PRICE_ATOMIC), "unit": "per signed HRV/RMSSD outcome receipt (no license)"},
            "networks": [X402_NETWORK],
            "accepts": [
                x402_accepts(TRACK_PRICE_ATOMIC, f"{BASE_URL}/stream", "Wellness music track for calm/focus sessions (MP3)"),
                x402_accepts(ALBUM_PRICE_ATOMIC, f"{BASE_URL}/stream", "Wellness music album for calm/focus sessions, 3 tracks (MP3)"),
                x402_accepts(SESSION_PLAY_PRICE_ATOMIC, f"{BASE_URL}/play", "Wellness music session play for calm/focus listening (MP3) — one play, no ownership"),
                x402_accepts(MEASUREMENT_VERIFY_PRICE_ATOMIC, f"{BASE_URL}/measure/verify", MEASURE_DESCRIPTION),
            ],
            "accepts_v2": [
                x402_accepts_v2(TRACK_PRICE_ATOMIC),
                x402_accepts_v2(ALBUM_PRICE_ATOMIC),
                x402_accepts_v2(SESSION_PLAY_PRICE_ATOMIC),
                x402_accepts_v2(MEASUREMENT_VERIFY_PRICE_ATOMIC),
            ],
            "facilitator": X402_FACILITATOR_URL,
            "stripe": {"enabled": False},
        },
        "catalog": {
            "tracks": len(TRACKS),
            "albums": len(ALBUMS),
            "url": f"{BASE_URL}/catalog",
            "evidence_sessions": SESSIONS_DOC["total_sessions"],
            "evidence_valid_sessions": SESSIONS_DOC["valid_sessions"],
        },
        "endpoints": {
            "catalog": {"url": f"{BASE_URL}/catalog", "method": "GET"},
            "recommend": {"url": f"{BASE_URL}/recommend", "method": "GET", "paywalled": False},
            "measure_verify": {"url": f"{BASE_URL}/measure/verify", "method": "POST", "paywalled": True},
            "stream": {"url": f"{BASE_URL}/stream", "methods": ["GET", "POST"], "paywalled": True, "mimeType": "audio/mpeg"},
            "play": {"url": f"{BASE_URL}/play", "methods": ["GET", "POST"], "paywalled": True, "mimeType": "audio/mpeg"},
            "pay_stripe": {"url": f"{BASE_URL}/pay/stripe", "method": "POST", "enabled": False},
            "verify_session": {"url": f"{BASE_URL}/verify-session", "method": "POST", "paywalled": False},
            "receipt_key": {"url": f"{BASE_URL}/.well-known/receipt-key", "method": "GET"},
            "x402_manifest": {"url": f"{BASE_URL}/.well-known/x402", "method": "GET"},
            "server_card": {"url": f"{BASE_URL}/mcp/server-card", "method": "GET"},
            "ai_catalog": {"url": f"{BASE_URL}/.well-known/ai-catalog.json", "method": "GET"},
            "health": {"url": f"{BASE_URL}/healthz", "method": "GET"},
        },
        "disclaimer": DISCLAIMER,
    }


@app.get("/mcp", tags=["mcp"])
def mcp_manifest():
    return manifest()


def _rpc_result(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_text(payload: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}


@app.post("/mcp", tags=["mcp"])
async def mcp_rpc(request: Request):
    """Minimal JSON-RPC stub: initialize, ping, tools/list, tools/call."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content=_rpc_error(None, -32700, "Parse error"))
    req_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    if method == "initialize":
        return _rpc_result(req_id, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lgda-autonomic-node", "version": "2.5.0"},
        })
    if method == "ping":
        return _rpc_result(req_id, {})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return JSONResponse(status_code=202, content={})
    if method == "tools/list":
        return _rpc_result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "list_catalog":
                return _rpc_result(req_id, _tool_text(CATALOG))
            if name == "get_track_evidence":
                track = track_or_404(args.get("track_id", ""))
                sessions = [s for s in SESSIONS_DOC["sessions"] if s["track_id"] == track["track_id"]]
                return _rpc_result(req_id, _tool_text({**track, "sessions": sessions}))
            if name == "recommend_track":
                return _rpc_result(req_id, _tool_text(recommend_tracks(
                    state=args.get("state", "session"),
                    limit=int(args.get("limit", 3)),
                    exclude=args.get("exclude") or [],
                )))
            if name == "purchase_track":
                return _rpc_result(req_id, _tool_text(track_purchase(args["track_id"], args["session_id"])))
            if name == "purchase_album":
                return _rpc_result(req_id, _tool_text(album_purchase(args["album_id"], args["session_id"])))
            if name == "play_track_session":
                return _rpc_result(req_id, _tool_text(track_session_play(args["track_id"], args["session_id"])))
            if name == "verify_session":
                req = VerifySessionRequest(**args)
                return _rpc_result(req_id, _tool_text(build_receipt(req)))
        except HTTPException as exc:
            return JSONResponse(status_code=200, content=_rpc_error(req_id, -32602, str(exc.detail)))
        except Exception as exc:
            return JSONResponse(status_code=200, content=_rpc_error(req_id, -32602, f"Invalid params: {exc}"))
        return JSONResponse(status_code=200, content=_rpc_error(req_id, -32601, f"Unknown tool: {name}"))
    return JSONResponse(status_code=200, content=_rpc_error(req_id, -32601, f"Method not found: {method}"))
