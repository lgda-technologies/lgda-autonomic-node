# Smithery — draft listing

**Status: CLOSED — Smithery shut down submissions on October 31, 2026.**
Verified 2026-11-17 from Smithery's own announcement
(https://www.smithery.ai/future): "On October 31, 2026, we're shutting
down Smithery's connection management services." What stopped working:
Smithery accounts and API keys, the Smithery API, Connect, Toolbox, and
the CLI and SDKs. https://smithery.ai/new returns "Not found" and the
site has no sign-in and no publishing flow. Smithery remains only as a
public directory of MCP servers, now powered by Arcade.dev, holding "a
curated set of verified servers" — with no published route for new
servers to join. (This also explains the CLI auth failure: the auth
endpoint https://smithery.ai/api/auth/cli/session returns 404.) The
only route the site offers is contacting cofounder Anirudh Kamath at
ani [at] smithery [dot] ai. Arcade.dev is not a substitute public
listing: registering a remote server there scopes it to your own
project/org; its public catalog entries are Arcade-verified, not
self-serve. Note: smithery.yaml stays in the repo (harmless), and the
official MCP Registry listing feeds directories that import from it
(Glama, API Tracker, PulseMCP).

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
