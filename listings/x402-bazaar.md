# x402 Bazaar — draft listing

**Status: DRAFT — submit only after v2 is deployed and the audio files are loaded.**
Bazaar: https://www.x402bazaar.com (x402 ecosystem directory for paid services).

## Listing

- **Name:** LGDA Autonomic Pacing Node
- **Tagline:** Wellness music for agents — 27 tracks, paid in USDC, delivered as MP3.
- **Category:** Audio / Music / Wellness
- **URL:** https://lgda-music.fly.dev
- **MCP endpoint:** https://lgda-music.fly.dev/mcp
- **Docs:** https://lgda-music.fly.dev/docs
- **Repository:** https://github.com/lgda-technologies/lgda-autonomic-node

## Description

LGDA Technologies sells wellness music direct to AI agents — no human
distribution in the loop. Agents discover the catalog through the MCP
manifest, pay per track or album with x402 (USDC), and receive the real
MP3 stream. Each track ships with its History session evidence:
camera-measured RMSSD and heart-rate readings from real listening
sessions (28 sessions on record; tracks without sessions honestly
report zero).

The camera heart-rate session itself stays on the human website
(https://lgda-technologies.base44.app) — it needs a person's fingertip
on the lens. Agents can submit pre/post HRV telemetry to
`/verify-session` and receive an Ed25519-signed receipt.

## Pricing (x402, scheme `exact`)

| Item | Price (USDC) | Atomic (6 dp) |
|---|---|---|
| Single track (MP3) | $5.99 | 5,990,000 |
| Album — 3 tracks (MP3) | $14.99 | 14,990,000 |

- Network: `base` (mainnet, live since 2026-11-17)
- Facilitator: https://x402.org/facilitator
- Paywalled resource: `GET|POST /stream` — returns HTTP 402 with the
  x402 `accepts` payload when no `X-PAYMENT` header is present.

## How an agent buys (for the listing's "How it works")

1. `GET /mcp` — read the manifest (5 tools, pricing, endpoints).
2. `tools/call purchase_track` (or `purchase_album`) — get the stream URL + x402 requirements.
3. `GET /stream?track_id=...&session_id=...` with `X-PAYMENT` — receive the MP3 + an `X-LGDA-Session` token.
4. `POST /verify-session` with pre/post HRV — receive a signed receipt.

## Disclaimer (include verbatim)

> Wellness music. Not a medical device; rPPG-derived HRV values are estimates; for research and educational use only.

## Submission checklist

- [x] v2 deployed, `/healthz` reports `audio_files_loaded: 27` (verified live 2026-11-17: 27 loaded, 0 missing; FDCD = Won't Shake, Out The Window, The Antidote)
- [x] Testnet vs mainnet decided; wallet confirmed by owner (mainnet, live 2026-11-17)
- [x] A test x402 payment verified end-to-end on the live node (track + album flows, testnet, 2026-11-17)
