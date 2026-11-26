"""Citizen loop tests: register, earn, spend, metrics."""
import os
import sys
import tempfile
from pathlib import Path

# Isolate key/audio/ledger BEFORE importing the app (same pattern as
# test_node.py; whichever test module imports first wins).
TMP = Path(tempfile.mkdtemp(prefix="lgda-citizen-test-"))
os.environ.setdefault("RECEIPT_KEY_FILE", str(TMP / "receipt_key.json"))
os.environ.setdefault("X402_TRUST_KEY_FILE", str(TMP / "x402_trust_key.json"))
os.environ.setdefault("AUDIO_DIR", str(TMP / "audio"))
(TMP / "audio").mkdir(exist_ok=True)
os.environ.setdefault("CITIZEN_LEDGER_FILE", str(TMP / "citizen_ledger.json"))
os.environ.setdefault("X402_NETWORK", "base-sepolia")
os.environ.setdefault("PAY_TO_ADDRESS", "0x8da12fca05e38aeda66f112fcf2ef6036c7bd666")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _fresh_ledger(tmp_path):
    main_module.CITIZEN_LEDGER_FILE = tmp_path / "citizen_ledger.json"


def test_citizen_loop(tmp_path, monkeypatch):
    _fresh_ledger(tmp_path)
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    monkeypatch.setattr(main_module, "AUDIO_DIR", audio_dir)
    (audio_dir / "midnight.mp3").write_bytes(b"ID3-test")

    r = client.post("/citizens/register", json={"agent_id": "agent-1", "role": "session"})
    assert r.status_code == 200
    assert r.json()["citizen"]["balance"] == 0

    r = client.post("/citizens/play", json={"agent_id": "agent-1", "track_id": "midnight", "session_id": "s1"})
    assert r.status_code == 402

    r = client.post("/citizens/work", json={"agent_id": "agent-1", "work_type": "discovery", "evidence": "https://example.com/listing-live"})
    assert r.status_code == 200
    assert r.json()["balance"] == 1

    r = client.post("/citizens/work", json={"agent_id": "agent-1", "work_type": "discovery", "evidence": "https://example.com/listing-live"})
    assert r.status_code == 409

    r = client.post("/citizens/play", json={"agent_id": "agent-1", "track_id": "midnight", "session_id": "s1"})
    assert r.status_code == 200
    assert r.headers["X-LGDA-Credit-Play"] == "credit-paid"
    assert r.headers["X-LGDA-Credit-Balance"] == "0"

    r = client.post("/citizens/outcome", json={"agent_id": "agent-1", "session_id": "s1", "track_id": "midnight", "pre_rmssd_ms": 40.0, "post_rmssd_ms": 55.0})
    assert r.status_code == 200
    assert r.json()["receipt"]["license_granted"] is False
    assert r.json()["receipt"]["rmssd_delta_ms"] == 15.0

    r = client.get("/citizens/metrics")
    assert r.status_code == 200
    assert r.json()["credit_plays"] == 1
    assert r.json()["credits_issued"] == 1
