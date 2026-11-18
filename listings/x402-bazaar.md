# x402 Bazaar — draft listing

**Status: DRAFT — submit only after v2 is deployed and the audio files are loaded.**
Bazaar: the x402 discovery layer. IMPORTANT (verified 2026-11-17):
`www.x402bazaar.com` is a parked domain for sale — it is NOT the Bazaar.
The real Bazaar is Coinbase's discovery index (browse at
https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources, public,
no key needed) and it has NO submission form: a resource is cataloged
automatically the first time a payment for it settles through a
facilitator that runs a discovery index (CDP or x402.org). Each index is
independent — settling through one lists you on that one only. For
richer discovery metadata, the 402 response should carry the `bazaar`
extension (x402 v2); our node declares it since v2.2.0 (see below).
The manual directory alternative is x402scan
(https://www.x402scan.com/resources/register — paste a URL, it validates
the 402 and lists it, no account needed).

## x402scan submission — LISTED (2026-11-17)

First attempt FAILED: "x402 v1 response detected — migrate to v2 spec"
(the node spoke v1 only), plus warnings for a missing /favicon.ico and
no info.contact.email in openapi.json. Fixed in v2.1.0 (dual-serve v2
header + v1 body, favicon, contact email) and resubmitted — SUCCEEDED:
**https://www.x402scan.com/server/12a3231a-7a98-426c-9939-f7ca68d09af8**
("LGDA Autonomic Pacing Node", POST /stream, $5.99, v2 badge, favicon
showing, no warnings). Activity is 0 transactions / $0.00 so far — the
listing is live, waiting for the first real buyer.

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

## Bazaar extension — LIVE in v2.2.0 (2026-11-17)

The v2 402 now carries the `bazaar` extension (info: HTTP GET /stream,
queryParams track_id/album_id + session_id, output binary audio/mpeg;
schema: Draft 2020-12, info validated against it in tests). Verified
live on mainnet: the PAYMENT-REQUIRED header decodes with
extensions.bazaar present. A v2 testnet purchase with a client echoing
the extension settled cleanly (tx 0x9a14ce1e...), proving the x402.org
facilitator accepts bazaar-extended payloads even though its /supported
list does not name the extension.

Caveat (verified 2026-11-17): the x402.org facilitator does NOT expose
the optional discovery endpoints — GET /discovery/resources and
/discovery/search both return the site's 404 page. So whether it
catalogs the resource internally cannot be checked from outside, and
the CDP index only catalogs mainnet settlements. The extension is the
spec's declared discovery mechanism and is live; the first real
(mainnet) sale is still the moment to check the CDP index for the
listing.
