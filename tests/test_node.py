"""Smoke tests for the LGDA node v2.

Run:  pip install -r requirements-dev.txt && python -m pytest tests/ -v
"""
import base64
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

# Isolate the receipt key + audio dir BEFORE importing the app (the app
# generates/loads its key at import time).
TMP = Path(tempfile.mkdtemp(prefix="lgda-test-"))
os.environ["RECEIPT_KEY_FILE"] = str(TMP / "receipt_key.json")
os.environ["X402_TRUST_KEY_FILE"] = str(TMP / "x402_trust_key.json")
os.environ["AUDIO_DIR"] = str(TMP / "audio")
(TMP / "audio").mkdir(exist_ok=True)
os.environ.setdefault("X402_NETWORK", "base-sepolia")
os.environ.setdefault("PAY_TO_ADDRESS", "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, issue_session_token
import app.main as main_module  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "catalog.json").read_text())
SESSIONS = json.loads((ROOT / "sessions.json").read_text())

client = TestClient(app)


def test_catalog_loads_27_tracks_9_albums():
    r = client.get("/catalog")
    assert r.status_code == 200
    data = r.json()
    assert len(data["tracks"]) == 27
    assert len(data["albums"]) == 9
    assert len({t["track_id"] for t in data["tracks"]}) == 27
    assert sum(len(a["track_ids"]) for a in data["albums"]) == 27
    for track in data["tracks"]:
        assert track["album_id"] in {a["album_id"] for a in data["albums"]}


def test_pricing_math():
    # $5.99/track and $14.99/album, 6-decimal USDC atomic units.
    assert CATALOG["track_price_usdc_atomic"] == 5_990_000
    assert CATALOG["album_price_usdc_atomic"] == 14_990_000
    assert CATALOG["track_price_usdc_atomic"] == int(round(5.99 * 1_000_000))
    assert CATALOG["album_price_usdc_atomic"] == int(round(14.99 * 1_000_000))
    # v2.5.0: $0.05 session play (no ownership).
    assert CATALOG["session_play_price_usd"] == 0.05
    assert CATALOG["session_play_price_usdc_atomic"] == 50_000
    assert CATALOG["session_play_price_usdc_atomic"] == int(round(0.05 * 1_000_000))
    for track in CATALOG["tracks"]:
        assert track["price_usd"] == 5.99
        assert track["price_usdc_atomic"] == 5_990_000
    for album in CATALOG["albums"]:
        assert album["price_usd"] == 14.99
        assert album["price_usdc_atomic"] == 14_990_000


def test_manifest_tools_exist():
    r = client.get("/mcp")
    assert r.status_code == 200
    m = r.json()
    assert m["schema_version"] == "lgda-mcp-manifest/1"
    assert m["name"] == "lgda-autonomic-node"
    assert m["version"] == "2.5.0"
    names = {t["name"] for t in m["tools"]}
    assert names == {
        "list_catalog",
        "get_track_evidence",
        "purchase_track",
        "purchase_album",
        "play_track_session",
        "verify_session",
        "recommend_track",
    }
    # v1's synthetic-tone tool must be gone.
    assert "stream_autonomic_frequency" not in names
    assert m["pricing"]["tracks"]["amount_atomic"] == "5990000"
    assert m["pricing"]["albums"]["amount_atomic"] == "14990000"
    assert m["pricing"]["session_plays"]["amount_atomic"] == "50000"
    assert m["pricing"]["measurement_verification"]["amount_atomic"] == "50000"
    assert m["endpoints"]["recommend"]["paywalled"] is False
    assert m["endpoints"]["measure_verify"]["paywalled"] is True
    assert m["endpoints"]["play"]["url"] == "https://lgda-music.fly.dev/play"


def test_evidence_aggregates_match_sessions():
    """Recompute every per-track aggregate from sessions.json and compare."""
    sessions = SESSIONS["sessions"]
    assert len(sessions) == 28
    assert SESSIONS["valid_sessions"] == 24
    assert SESSIONS["excluded_sessions"] == 4
    for track in CATALOG["tracks"]:
        tid = track["track_id"]
        rows = [s for s in sessions if s["track_id"] == tid]
        valid = [s for s in rows if s["valid"]]
        ev = track["evidence"]
        assert ev["session_count"] == len(rows), tid
        assert ev["valid_sessions"] == len(valid), tid
        if not valid:
            assert ev["mean_rmssd_ms"] is None, tid
            assert ev["mean_heart_rate_bpm"] is None, tid
            continue
        rmssd = [s["rmssd_ms"] for s in valid]
        hr = [s["heart_rate_bpm"] for s in valid if s["heart_rate_bpm"] is not None]
        assert ev["mean_rmssd_ms"] == round(statistics.mean(rmssd), 1), tid
        assert ev["median_rmssd_ms"] == round(statistics.median(rmssd), 1), tid
        assert ev["min_rmssd_ms"] == min(rmssd), tid
        assert ev["max_rmssd_ms"] == max(rmssd), tid
        assert ev["mean_heart_rate_bpm"] == round(statistics.mean(hr), 1), tid
    # Spot-check the headline numbers (see EVIDENCE.md for working).
    by_id = {t["track_id"]: t["evidence"] for t in CATALOG["tracks"]}
    assert by_id["midnight"]["valid_sessions"] == 4
    assert by_id["midnight"]["mean_rmssd_ms"] == 140.5
    assert by_id["higher-ground"]["valid_sessions"] == 9
    assert by_id["higher-ground"]["mean_rmssd_ms"] == 520.8
    assert by_id["the-golden-child"]["mean_rmssd_ms"] == 429.8
    # 21 tracks have no sessions at all.
    assert sum(1 for t in CATALOG["tracks"] if t["evidence"]["session_count"] == 0) == 21


def test_homepage_is_real():
    r = client.get("/")
    assert r.status_code == 200
    assert "not the API" in r.text
    # v1 returned exactly {"detail": "Not Found"} here; v2 serves HTML.
    assert r.text.strip() != '{"detail": "Not Found"}'
    assert "<html" in r.text
    assert "/catalog" in r.text and "/mcp" in r.text


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"] == "2.5.0"
    assert r.json()["x402_versions"] == [1, 2]
    assert r.json()["tracks"] == 27
    assert r.json()["audio_files_loaded"] == 0  # test audio dir is empty


def test_stream_missing_audio_unpaid_is_402_not_503():
    """v2.4.0: the 402 comes BEFORE validation. An unpaid request for
    a track whose audio is not loaded gets the payment challenge, not
    a 503 — probes and crawlers must see a 402. The 503 still applies
    once payment is presented (see test_paid_missing_audio_is_503_
    without_settlement): the node never settles for audio it cannot
    deliver."""
    r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
    assert r.status_code == 402
    assert r.json()["x402Version"] == 1


def test_stream_unknown_track_unpaid_is_402_not_404():
    """v2.4.0: same ordering as above — an unpaid request for an
    unknown track gets the 402, not a 404. The 404 still applies once
    payment is presented (see test_paid_unknown_track_is_404_without_
    settlement)."""
    r = client.get("/stream", params={"track_id": "nope", "session_id": "t1"})
    assert r.status_code == 402


def test_stream_paywall_402_then_token_serves_file(tmp_path=None):
    # Drop a fake MP3 into the test audio dir so the paywall path is reachable.
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    try:
        r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
        assert r.status_code == 402
        body = r.json()
        assert body["x402Version"] == 1
        assert body["error"] == "X-PAYMENT header is required"
        assert body["accepts"][0]["maxAmountRequired"] == "5990000"
        assert body["accepts"][0]["payTo"] == "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
        # A signed session token (as issued after a verified payment)
        # grants the file.
        token = issue_session_token("t1", ["midnight"], "0xtx")
        r2 = client.get(
            "/stream",
            params={"track_id": "midnight", "session_id": "t1"},
            headers={"X-LGDA-Session": token},
        )
        assert r2.status_code == 200
        assert r2.headers["content-type"].startswith("audio/mpeg")
        assert r2.content == b"ID3fake-mp3-bytes"
    finally:
        audio.unlink(missing_ok=True)


def test_verify_session_receipt_signature_verifies():
    key = client.get("/.well-known/receipt-key").json()
    assert key["key_id"].startswith("ed25519:")
    payload = {
        "session_id": "s1",
        "agent_id": "agent-1",
        "track_id": "midnight",
        "pre": {"rmssd_ms": 70.0, "source": "rppg"},
        "post": {"rmssd_ms": 90.0, "source": "rppg"},
    }
    r = client.post("/verify-session", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["receipt"]["rmssd_delta_ms"] == 20.0
    assert body["receipt"]["track_ids"] == ["midnight"]
    assert body["key_id"] == key["key_id"]
    # Verify the Ed25519 signature against the published public key.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(key["public_key_b64"]))
    canonical = json.dumps(body["receipt"], sort_keys=True, separators=(",", ":")).encode()
    pub.verify(base64.b64decode(body["signature_b64"]), canonical)  # raises if bad


def test_mcp_jsonrpc_tools_list_and_call():
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    assert len(r.json()["result"]["tools"]) == 7
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_track_evidence", "arguments": {"track_id": "midnight"}},
    })
    text = r.json()["result"]["content"][0]["text"]
    assert json.loads(text)["evidence"]["valid_sessions"] == 4
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "purchase_album", "arguments": {"album_id": "porter-ave", "session_id": "s1"}},
    })
    purchase = json.loads(r.json()["result"]["content"][0]["text"])
    assert purchase["price_usdc_atomic"] == 14_990_000
    assert purchase["track_ids"] == ["blue-beamer", "higher-ground", "tripper"]
    assert len(purchase["stream_urls"]) == 3


def test_stripe_stub_disabled():
    r = client.post("/pay/stripe", json={"session_id": "s1", "track_id": "midnight"})
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_wording_never_clinical():
    # "clinically calibrated" and diagnose/treat claims must not appear
    # in any agent-facing surface.
    for url in ["/", "/catalog", "/mcp", "/healthz"]:
        body = client.get(url).text.lower()
        assert "clinically calibrated" not in body, url
        assert "diagnose" not in body, url
    assert "wellness music" in client.get("/mcp").text.lower()


def test_402_dual_serve_v1_body_v2_header():
    """Unpaid /stream returns the v1 object in the body AND the
    canonical v2 PaymentRequired in the PAYMENT-REQUIRED header."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    try:
        r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
        assert r.status_code == 402
        # v1 body (unchanged).
        body = r.json()
        assert body["x402Version"] == 1
        assert body["accepts"][0]["maxAmountRequired"] == "5990000"
        assert body["accepts"][0]["network"] == "base-sepolia"
        # v2 header.
        assert "payment-required" in r.headers
        v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
        assert v2["x402Version"] == 2
        assert v2["error"] == "PAYMENT-SIGNATURE header is required"
        assert v2["resource"]["mimeType"] == "audio/mpeg"
        assert v2["resource"]["serviceName"] == "LGDA Music"
        assert v2["resource"]["iconUrl"].endswith("/favicon.ico")
        acc = v2["accepts"][0]
        assert acc["scheme"] == "exact"
        assert acc["network"] == "eip155:84532"  # CAIP-2, not "base-sepolia"
        assert acc["amount"] == "5990000"  # v2 field name
        assert "maxAmountRequired" not in acc
        assert acc["asset"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        assert acc["payTo"] == "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
        assert acc["extra"] == {"name": "USDC", "version": "2"}
    finally:
        audio.unlink(missing_ok=True)


def test_402_extra_name_per_network():
    """The 402's EIP-712 domain name follows the network: mainnet
    USDC's domain is "USD Coin", the base-sepolia test token's is
    "USDC" (both verified against the on-chain DOMAIN_SEPARATOR).
    v2.2.0 served "USDC" on both, which would have failed mainnet
    settlement. Applies to the v1 body AND the v2 header."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    old_network = main_module.X402_NETWORK
    try:
        for network, name, asset, caip2 in [
            ("base-sepolia", "USDC",
             "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "eip155:84532"),
            ("base", "USD Coin",
             "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "eip155:8453"),
        ]:
            main_module.X402_NETWORK = network
            r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
            assert r.status_code == 402
            assert r.json()["accepts"][0]["extra"] == {"name": name, "version": "2"}
            assert r.json()["accepts"][0]["asset"] == asset
            assert r.json()["accepts"][0]["network"] == network
            v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
            acc = v2["accepts"][0]
            assert acc["extra"] == {"name": name, "version": "2"}
            assert acc["asset"] == asset
            assert acc["network"] == caip2
    finally:
        main_module.X402_NETWORK = old_network
        audio.unlink(missing_ok=True)


def test_favicon_and_openapi_contact():
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    spec = client.get("/openapi.json").json()
    assert spec["info"]["contact"]["email"] == "solheefinery@gmail.com"


def _fake_facilitator(calls, version):
    """Stand-in for _facilitator_post: records calls, always approves."""
    def fake(path, payload):
        calls.append((path, payload))
        assert payload["x402Version"] == version
        if path == "/verify":
            return {"isValid": True, "payer": "0xbuyer"}
        return {"success": True, "transaction": "0xfake-tx", "network": payload["paymentRequirements"]["network"], "payer": "0xbuyer"}
    return fake


def test_v2_payment_accepted(monkeypatch):
    """A v2 PAYMENT-SIGNATURE payment settles (mocked facilitator) and
    the server returns both settlement headers + a session token."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    try:
        r402 = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
        v2 = json.loads(base64.b64decode(r402.headers["payment-required"]))
        payload = {
            "x402Version": 2,
            "resource": v2["resource"],
            "accepted": v2["accepts"][0],
            "payload": {
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xbuyer", "to": "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666",
                    "value": "5990000", "validAfter": "1", "validBefore": "9999999999",
                    "nonce": "0x" + "cd" * 32,
                },
            },
        }
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"},
                       headers={"PAYMENT-SIGNATURE": header})
        assert r.status_code == 200
        assert r.content == b"ID3fake-mp3-bytes"
        assert "x-lgda-session" in r.headers
        # Both settlement headers are present and decode.
        resp = json.loads(base64.b64decode(r.headers["payment-response"]))
        assert resp["success"] is True
        assert resp["transaction"] == "0xfake-tx"
        assert resp["network"] == "eip155:84532"
        assert resp["payer"] == "0xbuyer"
        assert json.loads(base64.b64decode(r.headers["x-payment-response"]))["success"] is True
        # The facilitator saw v2-shaped requirements.
        assert [p for p, _ in calls] == ["/verify", "/settle"]
        reqs = calls[0][1]["paymentRequirements"]
        assert reqs["network"] == "eip155:84532"
        assert reqs["amount"] == "5990000"
        assert calls[0][1]["paymentPayload"]["accepted"] == v2["accepts"][0]
    finally:
        audio.unlink(missing_ok=True)


def test_v1_payment_still_accepted(monkeypatch):
    """Dual-serve regression: an old v1 X-PAYMENT payment still works."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 1))
    try:
        r402 = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
        v1req = r402.json()["accepts"][0]
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base-sepolia",
            "payload": {
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xbuyer", "to": "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666",
                    "value": "5990000", "validAfter": "1", "validBefore": "9999999999",
                    "nonce": "0x" + "cd" * 32,
                },
            },
        }
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"},
                       headers={"X-PAYMENT": header})
        assert r.status_code == 200
        assert r.content == b"ID3fake-mp3-bytes"
        reqs = calls[0][1]["paymentRequirements"]
        assert reqs["network"] == "base-sepolia"
        assert reqs["maxAmountRequired"] == "5990000"
        assert reqs == v1req
    finally:
        audio.unlink(missing_ok=True)


def test_mcp_purchase_helpers_expose_v2():
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "purchase_track", "arguments": {"track_id": "midnight", "session_id": "s1"}},
    })
    purchase = json.loads(r.json()["result"]["content"][0]["text"])
    assert purchase["x402"]["x402Version"] == 1
    assert purchase["x402_v2"]["x402Version"] == 2
    assert purchase["x402_v2"]["accepts"][0]["network"] == "eip155:84532"
    assert purchase["x402_v2"]["accepts"][0]["amount"] == "5990000"


def test_bazaar_extension_in_v2_402():
    """The v2 402 carries the `bazaar` discovery extension and its
    `info` validates against its own bundled schema (what a
    facilitator does before cataloging)."""
    import jsonschema

    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    try:
        r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
        assert r.status_code == 402
        v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
        baz = v2["extensions"]["bazaar"]
        assert baz["info"]["input"]["type"] == "http"
        assert baz["info"]["input"]["method"] == "GET"
        assert baz["info"]["input"]["queryParams"] == {
            "track_id": "midnight", "session_id": "t1"}
        assert baz["info"]["output"]["type"] == "binary"
        assert baz["info"]["output"]["format"] == "audio/mpeg"
        jsonschema.validate(instance=baz["info"], schema=baz["schema"])
    finally:
        audio.unlink(missing_ok=True)


def test_bazaar_extension_album_402():
    """An album 402 declares album_id (not track_id) in the bazaar
    queryParams, and the info still validates against its schema."""
    import jsonschema

    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    try:
        r = client.get("/stream", params={
            "track_id": "midnight", "session_id": "t2",
            "album_id": "black-foot-ronin-vol-two"})
        assert r.status_code == 402
        v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
        baz = v2["extensions"]["bazaar"]
        assert baz["info"]["input"]["queryParams"] == {
            "album_id": "black-foot-ronin-vol-two", "session_id": "t2"}
        jsonschema.validate(instance=baz["info"], schema=baz["schema"])
    finally:
        audio.unlink(missing_ok=True)


# ---------------------------------------------------------------- v2.4.0

def _v2_payment_header(amount="5990000"):
    """A well-shaped v2 payment payload (the mocked facilitator does
    not check the signature, only the version + requirements)."""
    payload = {
        "x402Version": 2,
        "resource": {"url": "https://lgda-music.fly.dev/stream"},
        "accepted": {
            "scheme": "exact", "network": "eip155:84532",
            "amount": amount,
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "payTo": "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666",
        },
        "payload": {
            "signature": "0x" + "ab" * 65,
            "authorization": {
                "from": "0xbuyer", "to": "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666",
                "value": amount, "validAfter": "1", "validBefore": "9999999999",
                "nonce": "0x" + "cd" * 32,
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_stream_bare_returns_402():
    """R1: bare GET /stream returns the x402 402, not a 422. Probes
    and crawlers (x402gle audition) hit this URL with no params."""
    r = client.get("/stream")
    assert r.status_code == 402
    body = r.json()
    assert body["x402Version"] == 1
    assert body["accepts"][0]["maxAmountRequired"] == "5990000"
    assert body["accepts"][0]["extra"] == {"name": "USDC", "version": "2"}
    assert body["accepts"][0]["resource"] == "https://lgda-music.fly.dev/stream"
    v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert v2["x402Version"] == 2
    assert v2["accepts"][0]["amount"] == "5990000"
    assert v2["accepts"][0]["extra"] == {"name": "USDC", "version": "2"}


def test_stream_bare_post_returns_402():
    r = client.post("/stream")
    assert r.status_code == 402
    assert r.json()["x402Version"] == 1


def test_paid_unknown_track_is_404_without_settlement(monkeypatch):
    """R1 hard invariant: a payment for an unknown track verifies but
    is NEVER settled. The facilitator sees /verify only."""
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/stream", params={"track_id": "nope", "session_id": "t1"},
                   headers={"PAYMENT-SIGNATURE": _v2_payment_header()})
    assert r.status_code == 404
    assert [p for p, _ in calls] == ["/verify"]


def test_paid_missing_audio_is_503_without_settlement(monkeypatch):
    """R1 hard invariant: a payment for a track whose audio is not
    loaded verifies but is NEVER settled (503, no /settle call)."""
    (TMP / "audio" / "midnight.mp3").unlink(missing_ok=True)
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"},
                   headers={"PAYMENT-SIGNATURE": _v2_payment_header()})
    assert r.status_code == 503
    assert r.json()["detail"] == "audio file not yet loaded for this track"
    assert [p for p, _ in calls] == ["/verify"]


def test_paid_missing_params_is_400_without_settlement(monkeypatch):
    """R1: payment presented with no track_id/session_id verifies but
    is never settled — there is nothing to deliver."""
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/stream", headers={"PAYMENT-SIGNATURE": _v2_payment_header()})
    assert r.status_code == 400
    assert [p for p, _ in calls] == ["/verify"]


def test_well_known_x402_manifest():
    """R2: /.well-known/x402 (and .json) list every paid resource."""
    for path in ["/.well-known/x402", "/.well-known/x402.json"]:
        r = client.get(path)
        assert r.status_code == 200, path
        m = r.json()
        assert m["x402Version"] == 2
        assert m["scheme"] == "exact"
        assert m["network"] == "eip155:84532"
        assert m["asset"] == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        assert m["payTo"] == "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666"
        # 27 track resources + 9 album resources + 27 session-play resources.
        assert len(m["resources"]) == 64, path
        track = next(x for x in m["resources"] if "track_id=midnight" in x["resource"] and "/stream?" in x["resource"])
        assert track["accepts"][0]["amount"] == "5990000"
        assert track["inputSchema"]["required"] == ["track_id", "session_id"]
        assert track["outputSchema"]["contentMediaType"] == "audio/mpeg"
        album = next(x for x in m["resources"] if "album_id=porter-ave" in x["resource"])
        assert album["accepts"][0]["amount"] == "14990000"
        assert "track_id=" in album["resource"]  # album pays on a track URL
        assert len(m["endpoints"]) == 4
        assert {e["price_usd"] for e in m["endpoints"]} == {5.99, 14.99, 0.05}


def test_well_known_x402_trust_txt():
    """R3: /.well-known/x402-trust.txt carries the v1 P-256 SPKI line
    (format verified against a live provider file, 2026-11-19)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    r = client.get("/.well-known/x402-trust.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    line = r.text.strip()
    assert line.startswith("x402-trust-verification=v1:")
    value = line.split("v1:", 1)[1]
    der = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    assert len(der) == 91  # P-256 SPKI DER
    assert der[:2] == b"\x30\x59"
    key = load_der_public_key(der)
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert key.curve.name == "secp256r1"


def test_server_card_and_ai_catalog():
    """R4: SEP-2127 server card + AI Catalog / ARD manifest."""
    r = client.get("/mcp/server-card")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/mcp-server-card+json")
    assert r.headers["access-control-allow-origin"] == "*"
    assert "max-age=3600" in r.headers["cache-control"]
    assert "etag" in r.headers
    card = r.json()
    assert card["$schema"] == "https://static.modelcontextprotocol.io/schemas/v1/server-card.schema.json"
    assert card["name"] == "io.github.lgda-technologies/lgda-autonomic-node"
    assert card["name"].count("/") == 1
    assert card["version"] == "2.5.0"
    assert len(card["description"]) <= 100
    assert card["remotes"][0]["type"] == "streamable-http"
    assert card["remotes"][0]["url"] == "https://lgda-music.fly.dev/mcp"
    assert "2025-06-18" in card["remotes"][0]["supportedProtocolVersions"]
    # Legacy scanner path serves the identical card.
    r2 = client.get("/.well-known/mcp/server-card.json")
    assert r2.status_code == 200
    assert r2.json() == card
    # Both catalog paths serve the identical manifest.
    for path in ["/.well-known/ai-catalog.json", "/.well-known/ard.json",
                 "/.well-known/mcp/catalog.json", "/.well-known/mcp.json"]:
        rc = client.get(path)
        assert rc.status_code == 200, path
        assert rc.headers["content-type"].startswith("application/ai-catalog+json")
        cat = rc.json()
        assert cat["specVersion"] == "1.0"
        assert len(cat["entries"]) == 1
        entry = cat["entries"][0]
        assert entry["type"] == "application/mcp-server-card+json"
        assert entry["url"] == "https://lgda-music.fly.dev/mcp/server-card"
        assert entry["identifier"].startswith("urn:air:lgda-music.fly.dev:mcp:")
        assert entry["displayName"] == "LGDA Autonomic Pacing Node"


def test_mcp_tools_have_titles_and_annotations():
    """R5: every tool carries a human-readable title and the four MCP
    annotation hints (Glama flagged both as missing)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    tools = r.json()["result"]["tools"]
    assert len(tools) == 7
    by_name = {t["name"]: t for t in tools}
    for name, tool in by_name.items():
        assert tool["title"].strip(), name
        ann = tool["annotations"]
        for hint in ["readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"]:
            assert isinstance(ann[hint], bool), (name, hint)
    # Read tools are read-only; purchase tools touch the outside world.
    assert by_name["list_catalog"]["annotations"]["readOnlyHint"] is True
    assert by_name["get_track_evidence"]["annotations"]["readOnlyHint"] is True
    assert by_name["purchase_track"]["annotations"]["openWorldHint"] is True
    assert by_name["purchase_track"]["annotations"]["readOnlyHint"] is False
    assert by_name["purchase_album"]["annotations"]["openWorldHint"] is True
    assert by_name["play_track_session"]["annotations"]["openWorldHint"] is True
    assert by_name["play_track_session"]["annotations"]["readOnlyHint"] is False
    assert by_name["play_track_session"]["annotations"]["idempotentHint"] is False
    assert by_name["verify_session"]["annotations"]["readOnlyHint"] is True


def test_openapi_x_guidance_and_x_payment_info():
    """R2 (x402gle): OpenAPI carries info.x-guidance and the /stream
    operations carry x-payment-info; track_id/session_id are
    documented as optional at the HTTP layer (402 comes first)."""
    spec = client.get("/openapi.json").json()
    assert "x-guidance" in spec["info"]
    assert "402" in spec["info"]["x-guidance"]
    assert spec["info"]["version"] == "2.5.0"
    for method in ["get", "post"]:
        op = spec["paths"]["/stream"][method]
        pay = op["x-payment-info"]
        assert pay["protocols"] == ["x402"]
        assert pay["price"]["amount"] == "5.99"
        assert pay["price"]["currency"] == "USD"
        params = {p["name"]: p for p in op["parameters"]}
        assert params["track_id"]["required"] is False
        assert params["session_id"]["required"] is False


# ---------------------------------------------------------------- v2.5.0

def test_play_bare_returns_402():
    """R1 for /play: bare GET /play returns the x402 402 at $0.05, not
    a 422. Probes and crawlers hit this URL with no params."""
    r = client.get("/play")
    assert r.status_code == 402
    body = r.json()
    assert body["x402Version"] == 1
    assert body["accepts"][0]["maxAmountRequired"] == "50000"
    assert body["accepts"][0]["extra"] == {"name": "USDC", "version": "2"}
    assert body["accepts"][0]["resource"] == "https://lgda-music.fly.dev/play"
    assert body["accepts"][0]["mimeType"] == "audio/mpeg"
    v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert v2["x402Version"] == 2
    assert v2["accepts"][0]["amount"] == "50000"
    assert v2["accepts"][0]["extra"] == {"name": "USDC", "version": "2"}
    assert v2["resource"]["mimeType"] == "audio/mpeg"


def test_play_bare_post_returns_402():
    r = client.post("/play")
    assert r.status_code == 402
    assert r.json()["x402Version"] == 1
    assert r.json()["accepts"][0]["maxAmountRequired"] == "50000"


def test_calm_play_alias_returns_402_with_calm_resource():
    r = client.get("/calm/play", params={"track_id": "midnight", "session_id": "t1"})
    assert r.status_code == 402
    body = r.json()
    assert body["accepts"][0]["resource"] == (
        "https://lgda-music.fly.dev/calm/play?track_id=midnight&session_id=t1"
    )
    assert "calm/focus" in body["accepts"][0]["description"]
    v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert v2["resource"]["url"] == (
        "https://lgda-music.fly.dev/calm/play?track_id=midnight&session_id=t1"
    )


def test_play_402_amount_and_per_network_name():
    """The /play 402 carries $0.05 (50000) and the correct per-network
    EIP-712 domain name — 'USD Coin' on mainnet, 'USDC' on
    base-sepolia (the v2.3.0 lesson, applied to the new route)."""
    old_network = main_module.X402_NETWORK
    try:
        for network, name, asset, caip2 in [
            ("base-sepolia", "USDC",
             "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "eip155:84532"),
            ("base", "USD Coin",
             "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "eip155:8453"),
        ]:
            main_module.X402_NETWORK = network
            r = client.get("/play", params={"track_id": "midnight", "session_id": "t1"})
            assert r.status_code == 402
            assert r.json()["accepts"][0]["maxAmountRequired"] == "50000"
            assert r.json()["accepts"][0]["extra"] == {"name": name, "version": "2"}
            assert r.json()["accepts"][0]["asset"] == asset
            assert r.json()["accepts"][0]["network"] == network
            v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
            acc = v2["accepts"][0]
            assert acc["amount"] == "50000"
            assert acc["extra"] == {"name": name, "version": "2"}
            assert acc["asset"] == asset
            assert acc["network"] == caip2
    finally:
        main_module.X402_NETWORK = old_network


def test_play_paid_unknown_track_is_404_without_settlement(monkeypatch):
    """Hard invariant for /play: a payment for an unknown track
    verifies but is NEVER settled. The facilitator sees /verify only."""
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/play", params={"track_id": "nope", "session_id": "t1"},
                   headers={"PAYMENT-SIGNATURE": _v2_payment_header("50000")})
    assert r.status_code == 404
    assert [p for p, _ in calls] == ["/verify"]


def test_play_paid_missing_audio_is_503_without_settlement(monkeypatch):
    """Hard invariant for /play: a payment for a track whose audio is
    not loaded verifies but is NEVER settled (503, no /settle call)."""
    (TMP / "audio" / "midnight.mp3").unlink(missing_ok=True)
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/play", params={"track_id": "midnight", "session_id": "t1"},
                   headers={"PAYMENT-SIGNATURE": _v2_payment_header("50000")})
    assert r.status_code == 503
    assert r.json()["detail"] == "audio file not yet loaded for this track"
    assert [p for p, _ in calls] == ["/verify"]


def test_play_paid_missing_params_is_400_without_settlement(monkeypatch):
    """Hard invariant for /play: payment presented with no
    track_id/session_id verifies but is never settled — there is
    nothing to deliver."""
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    r = client.get("/play", headers={"PAYMENT-SIGNATURE": _v2_payment_header("50000")})
    assert r.status_code == 400
    assert [p for p, _ in calls] == ["/verify"]


def test_play_v2_payment_accepted(monkeypatch):
    """A v2 PAYMENT-SIGNATURE $0.05 play settles (mocked facilitator)
    and returns the audio bytes — with the single-use terms headers
    and NO session token. The x402 settlement-confirmation headers
    ARE returned (protocol-level proof of payment, not an ownership
    receipt). A play grants nothing durable."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 2))
    try:
        r = client.get("/play", params={"track_id": "midnight", "session_id": "t1"},
                       headers={"PAYMENT-SIGNATURE": _v2_payment_header("50000")})
        assert r.status_code == 200
        assert r.content == b"ID3fake-mp3-bytes"
        assert r.headers["content-type"].startswith("audio/mpeg")
        # Terms headers state the deal.
        assert r.headers["x-lgda-session-play"] == "single-use"
        assert "no ownership" in r.headers["x-lgda-license"]
        # Nothing durable is granted — no session token.
        assert "x-lgda-session" not in r.headers
        # But the x402 settlement confirmation IS returned, so the
        # payer can verify the payment settled.
        resp = json.loads(base64.b64decode(r.headers["payment-response"]))
        assert resp["success"] is True
        assert resp["transaction"] == "0xfake-tx"
        assert "x-payment-response" in r.headers
        # The facilitator saw v2-shaped $0.05 requirements.
        assert [p for p, _ in calls] == ["/verify", "/settle"]
        reqs = calls[0][1]["paymentRequirements"]
        assert reqs["network"] == "eip155:84532"
        assert reqs["amount"] == "50000"
    finally:
        audio.unlink(missing_ok=True)


def test_play_v1_payment_accepted(monkeypatch):
    """Dual-serve for /play: an old v1 X-PAYMENT payment still works."""
    audio = TMP / "audio" / "midnight.mp3"
    audio.write_bytes(b"ID3fake-mp3-bytes")
    calls = []
    monkeypatch.setattr(main_module, "_facilitator_post", _fake_facilitator(calls, 1))
    try:
        r402 = client.get("/play", params={"track_id": "midnight", "session_id": "t1"})
        v1req = r402.json()["accepts"][0]
        payload = {
            "x402Version": 1,
            "scheme": "exact",
            "network": "base-sepolia",
            "payload": {
                "signature": "0x" + "ab" * 65,
                "authorization": {
                    "from": "0xbuyer", "to": "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666",
                    "value": "50000", "validAfter": "1", "validBefore": "9999999999",
                    "nonce": "0x" + "cd" * 32,
                },
            },
        }
        header = base64.b64encode(json.dumps(payload).encode()).decode()
        r = client.get("/play", params={"track_id": "midnight", "session_id": "t1"},
                       headers={"X-PAYMENT": header})
        assert r.status_code == 200
        assert r.content == b"ID3fake-mp3-bytes"
        assert r.headers["x-lgda-session-play"] == "single-use"
        assert "x-lgda-session" not in r.headers
        resp = json.loads(base64.b64decode(r.headers["payment-response"]))
        assert resp["success"] is True
        reqs = calls[0][1]["paymentRequirements"]
        assert reqs["network"] == "base-sepolia"
        assert reqs["maxAmountRequired"] == "50000"
        assert reqs == v1req
    finally:
        audio.unlink(missing_ok=True)


def test_play_bazaar_extension_in_v2_402():
    """The /play v2 402 carries the `bazaar` discovery extension and
    its `info` validates against its own bundled schema (what a
    facilitator does before cataloging)."""
    import jsonschema

    r = client.get("/play", params={"track_id": "midnight", "session_id": "t1"})
    assert r.status_code == 402
    v2 = json.loads(base64.b64decode(r.headers["payment-required"]))
    baz = v2["extensions"]["bazaar"]
    assert baz["info"]["input"]["type"] == "http"
    assert baz["info"]["input"]["method"] == "GET"
    assert baz["info"]["input"]["queryParams"] == {
        "track_id": "midnight", "session_id": "t1"}
    assert baz["info"]["output"]["type"] == "binary"
    assert baz["info"]["output"]["format"] == "audio/mpeg"
    jsonschema.validate(instance=baz["info"], schema=baz["schema"])


def test_well_known_x402_manifest_includes_session_plays():
    """v2.5.0: the manifest lists the 27 $0.05 session-play resources
    (both aliases), each clearly marked as no-ownership."""
    for path in ["/.well-known/x402", "/.well-known/x402.json"]:
        m = client.get(path).json()
        plays = [x for x in m["resources"] if "/play?" in x["resource"]]
        assert len(plays) == 27, path
        for p in plays:
            assert p["accepts"][0]["amount"] == "50000", path
            assert "no ownership" in p["description"], path
            assert p["inputSchema"]["required"] == ["track_id", "session_id"], path
            assert p["outputSchema"]["contentMediaType"] == "audio/mpeg", path
        play_ep = next(e for e in m["endpoints"] if e["path"] == "/play")
        assert play_ep["price_usd"] == 0.05
        assert play_ep["maxAmountRequired"] == "50000"


def test_mcp_play_track_session_tool():
    """v2.5.0: 6th MCP tool play_track_session, with title +
    annotations, returns the /play URL and $0.05 x402 requirements."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = r.json()["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "play_track_session")
    assert tool["title"].strip()
    assert tool["annotations"] == {
        "readOnlyHint": False, "destructiveHint": False,
        "idempotentHint": False, "openWorldHint": True}
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "play_track_session", "arguments": {"track_id": "midnight", "session_id": "s1"}},
    })
    play = json.loads(r.json()["result"]["content"][0]["text"])
    assert play["price_usd"] == 0.05
    assert play["price_usdc_atomic"] == 50_000
    assert play["play_url"] == "https://lgda-music.fly.dev/play?track_id=midnight&session_id=s1"
    assert play["x402"]["accepts"][0]["maxAmountRequired"] == "50000"
    assert play["x402_v2"]["accepts"][0]["amount"] == "50000"
    assert "no ownership" in play["terms"].lower()


def test_openapi_play_x_payment_info():
    """v2.5.0: the /play operations carry x-payment-info at $0.05 and
    document track_id/session_id as optional at the HTTP layer (402
    comes first). The info-level x-guidance names the session play."""
    spec = client.get("/openapi.json").json()
    assert "0.05" in spec["info"]["x-guidance"]
    assert "/play" in spec["info"]["x-guidance"]
    for method in ["get", "post"]:
        op = spec["paths"]["/play"][method]
        pay = op["x-payment-info"]
        assert pay["protocols"] == ["x402"]
        assert pay["price"]["amount"] == "0.05"
        assert pay["price"]["currency"] == "USD"
        assert pay["price"]["atomicAmount"] == "50000"
        params = {p["name"]: p for p in op["parameters"]}
        assert params["track_id"]["required"] is False
        assert params["session_id"]["required"] is False


def test_recommend_free_state_to_track():
    r = client.get("/recommend", params={"state": "focus", "limit": 3})
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "focus"
    assert data["privacy"]["payment_required"] is False
    assert data["privacy"]["biometric_input_required"] is False
    assert "does not prove" in data["evidence_note"]
    assert data["recommendations"][0]["track_id"] == "higher-ground"
    assert data["recommendations"][0]["play"]["price_usd"] == 0.05
    assert data["recommendations"][0]["purchase"]["price_usd"] == 5.99
    r = client.get("/recommend", params={"state": "wind-down", "limit": 2, "exclude": "higher-ground"})
    assert r.status_code == 200
    assert r.json()["state"] == "wind_down"
    assert all(x["track_id"] != "higher-ground" for x in r.json()["recommendations"])
    assert client.get("/recommend", params={"state": "bogus"}).status_code == 400


def test_mcp_recommend_track_tool():
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "recommend_track", "arguments": {"state": "calm", "limit": 2}},
    })
    assert r.status_code == 200
    rec = json.loads(r.json()["result"]["content"][0]["text"])
    assert rec["state"] == "calm"
    assert len(rec["recommendations"]) == 2
    assert rec["recommendations"][0]["evidence_status"] == "history_sessions"


def test_measure_verify_unpaid_returns_402_before_validation():
    r = client.post("/measure/verify", json={})
    assert r.status_code == 402
    assert r.json()["accepts"][0]["maxAmountRequired"] == "50000"
    m = client.get("/.well-known/x402").json()
    measure = [x for x in m["resources"] if x["resource"].endswith("/measure/verify")]
    assert len(measure) == 1
    assert measure[0]["accepts"][0]["amount"] == "50000"
    assert "no license" in measure[0]["description"]


def test_measure_verify_paid_returns_signed_outcome(monkeypatch):
    monkeypatch.setattr(main_module, "verify_payment", lambda *a, **k: ({}, {}, 2))
    monkeypatch.setattr(main_module, "settle_payment", lambda *a, **k: {"transaction": "0xtest", "payer": "0xpayer"})
    payload = {
        "session_id": "s-measure",
        "agent_id": "agent-1",
        "track_id": "higher-ground",
        "pre": {"rmssd_ms": 80.0, "source": "rppg"},
        "post": {"rmssd_ms": 95.0, "source": "rppg"},
    }
    r = client.post("/measure/verify", json=payload, headers={"PAYMENT-SIGNATURE": "test"})
    assert r.status_code == 200
    body = r.json()
    assert body["outcome"]["rmssd_delta_ms"] == 15.0
    assert body["outcome"]["license_granted"] is False
    assert body["outcome"]["proof_scope"] == "outcome_only_not_license"
    assert body["outcome"]["payment_tx"] == "0xtest"
    assert r.headers["PAYMENT-RESPONSE"]
