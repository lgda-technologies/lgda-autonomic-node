# Smithery — draft listing

**Status: DRAFT — submit only after v2 is deployed and the audio files are loaded.**
Smithery: https://smithery.ai (MCP server directory; listing via a
`smithery.yaml` in the server repo + registry submission).

## Listing

- **Name:** lgda-autonomic-node
- **Title:** LGDA Autonomic Pacing Node
- **Version:** 2.0.0
- **Description:** Buy LGDA Technologies wellness music with USDC — 27 tracks, 9 albums, delivered as MP3, with signed HRV session receipts. Agents pay per track ($5.99) or album ($14.99) via x402. Each track includes its History session evidence (RMSSD / heart rate).
- **Transport:** Streamable HTTP (remote server — no local install)
- **URL:** https://lgda-music.fly.dev/mcp
- **Repository:** https://github.com/lgda-technologies/lgda-autonomic-node

## Tools

- `list_catalog` — full catalog with prices + evidence (free)
- `get_track_evidence` — per-track session evidence (free)
- `purchase_track` — buy one track, $5.99 USDC (x402)
- `purchase_album` — buy one album (3 tracks), $14.99 USDC (x402)
- `verify_session` — signed HRV receipt for a session (free)

## Suggested `smithery.yaml` (add to repo root before submitting)

```yaml
# Smithery configuration for the LGDA node (remote, streamable HTTP).
# See https://smithery.ai/docs for the current schema at submission time.
startCommand:
  type: http
  url: "https://lgda-music.fly.dev/mcp"
```

Note: Smithery's format evolves — confirm the current `smithery.yaml`
schema and remote-server flow when submitting; the endpoint above is
the stable part.

## Pricing

x402 (USDC): $5.99 per track, $14.99 per album (3 tracks). Network
`base` (mainnet, live since 2026-11-17).

## Disclaimer (include verbatim)

> Wellness music. Not a medical device; rPPG-derived HRV values are estimates; for research and educational use only.

## Submission checklist

- [x] v2 deployed, `/healthz` reports `audio_files_loaded: 27` (verified live 2026-11-17: 27 loaded, 0 missing; FDCD = Won't Shake, Out The Window, The Antidote)
- [x] Code in a public GitHub repo with `smithery.yaml` — https://github.com/lgda-technologies/lgda-autonomic-node
- [x] Owner confirmed the payment wallet; mainnet vs testnet decided (mainnet, live 2026-11-17)
