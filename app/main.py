"""
LGDA Autonomic Pacing Node — v2.3.0.

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

Wording: "wellness music". Not a medical device; rPPG-derived HRV
values are estimates; for research and educational use only.
Never "clinically calibrated", never diagnose/treat claims.

Entitlement model (simple):
- A track purchase pays $5.99 and entitles the buyer to that one
  track. An album purchase pays $14.99 and entitles the buyer to
  all 3 tracks on that album.
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
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
except ImportError:  # pragma: no cover - cryptography is in requirements.txt
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

# ---------------------------------------------------------------- app

app = FastAPI(
    title="LGDA Autonomic Pacing Node",
    description=(
        "x402-paywalled wellness music catalog (27 tracks, 9 albums) "
        "with MCP discovery and signed HRV session receipts. "
        + DISCLAIMER
    ),
    version="2.3.0",
    contact={"name": "LGDA Technologies", "email": "solheefinery@gmail.com"},
)

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
    """x402 v2 `bazaar` discovery extension for the /stream endpoint.

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
            "User-Agent": "lgda-music-node/2.3.0"}) as client:
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


def verify_and_settle_payment(payment_header: str, price_atomic: int,
                              resource: str, description: str) -> dict:
    """Verify + settle an x402 payment via the facilitator.

    Version-aware: the payload's x402Version picks the facilitator
    dialect (v1: X-PAYMENT shape, plain network names; v2:
    PAYMENT-SIGNATURE shape, CAIP-2 network, `accepted` envelope).
    The EIP-3009 authorization inside is identical in both versions,
    and the facilitator accepts both. Returns the settle result (with
    transaction hash) or raises HTTPException 402. No audio is served
    on an unverified payment.

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
<h1>LGDA Autonomic Pacing Node (v2.3.0)</h1>
<p>Machine-readable front door for the LGDA Technologies wellness music
catalog — 27 tracks across 9 albums. AI agents discover this node via
the <a href="/mcp">MCP manifest</a>, pay per track ($5.99) or album
($14.99) with x402 (USDC), and receive the real MP3.</p>
<p><strong>This page (/) is not the API.</strong> (v1 returned
<code>{"detail": "Not Found"}</code> here — v2 serves this explanation
instead.)</p>
<ul>
<li><a href="/catalog">/catalog</a> — full catalog with per-track session evidence</li>
<li><a href="/mcp">/mcp</a> — MCP manifest: tools, pricing, endpoints</li>
<li><a href="/docs">/docs</a> — OpenAPI docs</li>
<li><a href="/healthz">/healthz</a> — health check</li>
<li><a href="/.well-known/receipt-key">/.well-known/receipt-key</a> — Ed25519 receipt key</li>
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
        "version": "2.3.0",
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

# ---------------------------------------------------------------- stream (paywalled)

def _stream(track_id: str, session_id: str, album_id: Optional[str],
            x_payment: Optional[str], payment_signature: Optional[str],
            x_session: Optional[str]):
    track = track_or_404(track_id)
    if album_id:
        album = album_or_404(album_id)
        if track_id not in album["track_ids"]:
            raise HTTPException(400, f"Track {track_id} is not on album {album_id}")

    # 1. Never take payment for a track whose audio is not loaded.
    path = audio_path(track_id)
    if not path.exists():
        raise HTTPException(503, "audio file not yet loaded for this track")

    # 2. Session token (issued after a verified payment) grants access.
    entitled: Optional[list] = None
    if x_session:
        token = parse_session_token(x_session)
        if token and track_id in token.get("track_ids", []):
            entitled = token["track_ids"]

    # 3. Otherwise a fresh x402 payment is required.
    payment_tx = None
    if entitled is None:
        if album_id:
            price, resource = ALBUM_PRICE_ATOMIC, f"{BASE_URL}/stream?album_id={album_id}&session_id={session_id}"
            desc = f"Wellness music album: {ALBUMS[album_id]['title']} (3 MP3 tracks)"
            grant = ALBUMS[album_id]["track_ids"]
        else:
            price, resource = TRACK_PRICE_ATOMIC, f"{BASE_URL}/stream?track_id={track_id}&session_id={session_id}"
            desc = f"Wellness music track: {track['title']} (MP3)"
            grant = [track_id]
        # v2 clients send PAYMENT-SIGNATURE; v1 clients send X-PAYMENT.
        # Either is accepted (dual-serve); v2 takes precedence if both
        # are present.
        payment_header = payment_signature or x_payment
        if not payment_header:
            return x402_402(price, resource, desc)
        settled = verify_and_settle_payment(payment_header, price, resource, desc)
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

    return FileResponse(path, media_type="audio/mpeg", filename=f"{track_id}.mp3")


@app.get("/stream", tags=["stream"])
def stream_get(track_id: str, session_id: str, album_id: Optional[str] = None,
               x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
               payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE"),
               x_session: Optional[str] = Header(default=None, alias="X-LGDA-Session")):
    return _stream(track_id, session_id, album_id, x_payment, payment_signature, x_session)


@app.post("/stream", tags=["stream"])
def stream_post(track_id: str, session_id: str, album_id: Optional[str] = None,
                x_payment: Optional[str] = Header(default=None, alias="X-PAYMENT"),
                payment_signature: Optional[str] = Header(default=None, alias="PAYMENT-SIGNATURE"),
                x_session: Optional[str] = Header(default=None, alias="X-LGDA-Session")):
    return _stream(track_id, session_id, album_id, x_payment, payment_signature, x_session)

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
        "version": "2.3.0",
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

# ---------------------------------------------------------------- MCP manifest + JSON-RPC

TOOLS = [
    {
        "name": "list_catalog",
        "title": "List the wellness music catalog",
        "description": "List all 9 albums and 27 wellness music tracks with prices ($5.99/track, $14.99/album) and per-track session evidence. No payment required.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
    },
    {
        "name": "verify_session",
        "title": "Verify a listening session and get a signed receipt",
        "description": "Submit pre/post HRV segments (RMSSD, IBI, source) for a paid session and receive an Ed25519-signed receipt. Not paywalled.",
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
    },
]


def manifest() -> dict:
    return {
        "schema_version": "lgda-mcp-manifest/1",
        "name": "lgda-autonomic-node",
        "title": "LGDA Autonomic Pacing Node",
        "version": "2.3.0",
        "description": (
            "Stateless M2M node: x402-paid wellness music (27 tracks, "
            "9 albums) + signed HRV session receipts. The music is the "
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
            "networks": [X402_NETWORK],
            "accepts": [
                x402_accepts(TRACK_PRICE_ATOMIC, f"{BASE_URL}/stream", "Wellness music track (MP3)"),
                x402_accepts(ALBUM_PRICE_ATOMIC, f"{BASE_URL}/stream", "Wellness music album, 3 tracks (MP3)"),
            ],
            "accepts_v2": [
                x402_accepts_v2(TRACK_PRICE_ATOMIC),
                x402_accepts_v2(ALBUM_PRICE_ATOMIC),
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
            "stream": {"url": f"{BASE_URL}/stream", "methods": ["GET", "POST"], "paywalled": True, "mimeType": "audio/mpeg"},
            "pay_stripe": {"url": f"{BASE_URL}/pay/stripe", "method": "POST", "enabled": False},
            "verify_session": {"url": f"{BASE_URL}/verify-session", "method": "POST", "paywalled": False},
            "receipt_key": {"url": f"{BASE_URL}/.well-known/receipt-key", "method": "GET"},
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
            "serverInfo": {"name": "lgda-autonomic-node", "version": "2.3.0"},
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
            if name == "purchase_track":
                return _rpc_result(req_id, _tool_text(track_purchase(args["track_id"], args["session_id"])))
            if name == "purchase_album":
                return _rpc_result(req_id, _tool_text(album_purchase(args["album_id"], args["session_id"])))
            if name == "verify_session":
                req = VerifySessionRequest(**args)
                return _rpc_result(req_id, _tool_text(build_receipt(req)))
        except HTTPException as exc:
            return JSONResponse(status_code=200, content=_rpc_error(req_id, -32602, str(exc.detail)))
        except Exception as exc:
            return JSONResponse(status_code=200, content=_rpc_error(req_id, -32602, f"Invalid params: {exc}"))
        return JSONResponse(status_code=200, content=_rpc_error(req_id, -32601, f"Unknown tool: {name}"))
    return JSONResponse(status_code=200, content=_rpc_error(req_id, -32601, f"Method not found: {method}"))
