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
    assert m["version"] == "2.2.0"
    names = {t["name"] for t in m["tools"]}
    assert names == {
        "list_catalog",
        "get_track_evidence",
        "purchase_track",
        "purchase_album",
        "verify_session",
    }
    # v1's synthetic-tone tool must be gone.
    assert "stream_autonomic_frequency" not in names
    assert m["pricing"]["tracks"]["amount_atomic"] == "5990000"
    assert m["pricing"]["albums"]["amount_atomic"] == "14990000"


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
    assert r.json()["version"] == "2.2.0"
    assert r.json()["x402_versions"] == [1, 2]
    assert r.json()["tracks"] == 27
    assert r.json()["audio_files_loaded"] == 0  # test audio dir is empty


def test_stream_missing_audio_is_503_not_a_tone():
    r = client.get("/stream", params={"track_id": "midnight", "session_id": "t1"})
    assert r.status_code == 503
    assert r.json()["detail"] == "audio file not yet loaded for this track"


def test_stream_unknown_track_is_404():
    r = client.get("/stream", params={"track_id": "nope", "session_id": "t1"})
    assert r.status_code == 404


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
    assert len(r.json()["result"]["tools"]) == 5
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
