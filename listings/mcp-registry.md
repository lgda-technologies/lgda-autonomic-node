# MCP Registry — draft listing

**Status: DRAFT — submit only after v2 is deployed and the audio files are loaded.**
Registry: https://registry.modelcontextprotocol.io (publish via `mcp-publisher` / GitHub-based flow; see registry docs at submission time.)

## Server entry

- **Name:** `io.github.lgda-technologies/lgda-autonomic-node`
  (adjust to the actual GitHub org/repo once the code is pushed)
- **Title:** LGDA Autonomic Pacing Node
- **Version:** 2.1.0
- **Description:** Stateless machine-to-machine node selling LGDA Technologies wellness music to AI agents: 27 tracks across 9 albums, paid per track ($5.99) or album ($14.99) in USDC via x402, delivered as MP3, with signed HRV session receipts. Each track carries its History session evidence (RMSSD / heart rate).
- **Repository:** https://github.com/lgda-technologies/lgda-autonomic-node
- **Transport:** Streamable HTTP — `https://lgda-music.fly.dev/mcp`
- **Protocol versions:** 2025-06-18, 2025-03-26, 2024-11-05

## Tools

| Tool | Description |
|---|---|
| `list_catalog` | List all 9 albums / 27 tracks with prices and per-track session evidence. Free. |
| `get_track_evidence` | Session evidence for one track (session count, mean/median/min/max RMSSD, mean HR). Free. |
| `purchase_track` | Buy one track ($5.99 USDC, x402). Returns stream URL + payment requirements. |
| `purchase_album` | Buy one album ($14.99 USDC, x402). One payment entitles the buyer to all 3 tracks. |
| `verify_session` | Submit pre/post HRV segments, receive an Ed25519-signed receipt. Free, not paywalled. |

## Pricing

- x402, scheme `exact`, USDC (6 decimals)
- Track: $5.99 (5,990,000 atomic) per track
- Album: $14.99 (14,990,000 atomic) per album (3 tracks)
- Network: `base` (mainnet, live since 2026-11-17)
- Facilitator: https://x402.org/facilitator

## Endpoints

- Manifest: `GET https://lgda-music.fly.dev/mcp`
- JSON-RPC: `POST https://lgda-music.fly.dev/mcp`
- Catalog: `GET https://lgda-music.fly.dev/catalog`
- Stream (paywalled): `GET|POST https://lgda-music.fly.dev/stream?track_id=...&session_id=...`
- Receipts: `POST https://lgda-music.fly.dev/verify-session`
- Receipt key: `GET https://lgda-music.fly.dev/.well-known/receipt-key`
- Health: `GET https://lgda-music.fly.dev/healthz`

## Disclaimer (include verbatim)

> Wellness music. Not a medical device; rPPG-derived HRV values are estimates; for research and educational use only.

## Submission checklist

- [x] v2 deployed to https://lgda-music.fly.dev, `/healthz` reports `audio_files_loaded: 27` (verified live 2026-11-17: 27 loaded, 0 missing; FDCD = Won't Shake, Out The Window, The Antidote)
- [x] Code pushed to a public GitHub repo (registry requires it) — https://github.com/lgda-technologies/lgda-autonomic-node
- [x] Owner confirmed the payment wallet (mainnet, live 2026-11-17)
- [x] `GET /mcp` returns the v2 manifest (version 2.1.0, 5 tools)
