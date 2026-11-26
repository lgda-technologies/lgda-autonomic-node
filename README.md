# LGDA Autonomic Pacing Node — v2

[![LGDA Autonomic Pacing Node MCP connector](https://glama.ai/mcp/connectors/io.github.lgda-technologies/lgda-autonomic-node/badges/score.svg)](https://glama.ai/mcp/connectors/io.github.lgda-technologies/lgda-autonomic-node)
[![LGDA Autonomic Node MCP server](https://mcprush.com/badge/lgda-autonomic-node-mcp/card.svg)](https://mcprush.com/to/lgda-autonomic-node-mcp?ref=badge)
[![M8ven Score](https://m8ven.ai/badge/mcp/lgda-technologies-lgda-autonomic-node-wy8zzu?v=ae3cd0e4cfbf19ee44abbc824fb89acb)](https://m8ven.ai/mcp/lgda-technologies-lgda-autonomic-node-wy8zzu?s=readme)

Machine-readable front door for the **LGDA Technologies wellness music
catalog** (human storefront: https://lgda-technologies.base44.app).
AI agents discover this node via the `/mcp` manifest, pay per track or
album with x402 (USDC) and receive the real MP3 — or play one track
once in a session for $0.05 (no ownership) — no human in the loop.

**Try it live:** the [LGDA Room Prototype](https://lgda-technologies.github.io/lgda-room/)
runs a free session in your browser — camera in, music and light out —
so you can watch your own numbers move before you buy.

**The music is the product.** This node sells the actual 27-track
catalog. It never generates a synthetic tone.

> Wellness music. Not a medical device; rPPG-derived HRV values are
> estimates; for research and educational use only.

## What changed from v1

| | v1 (live at lgda-node.fly.dev) | v2 (this repo) |
|---|---|---|
| Product | One tool generating a synthetic paced-breathing tone | Real catalog: 27 tracks, 9 albums |
| Price | $0.05 per stream | $5.99/track, $14.99/album, $0.05/session play |
| Tools | `stream_autonomic_frequency` only | `list_catalog`, `get_track_evidence`, `purchase_track`, `purchase_album`, `play_track_session`, `verify_session` |
| Audio | Generated WAV tone | Real MP3 from `./audio/`; missing file → 503, never a tone |
| `GET /` | `{"detail": "Not Found"}` | Real homepage explaining the node |
| Evidence | None | Per-track History session data (RMSSD/HR) in the catalog |
| Payment | x402 base-sepolia, $0.05 | x402, configurable network; same testnet default until mainnet is confirmed |
| Stripe | Listed, disabled | Stub returning `{"enabled": false}` until implemented |

Kept from v1 where sensible: FastAPI, `/healthz`,
`/.well-known/receipt-key` (Ed25519), `POST /verify-session` with the
same pre/post `Segment` shape, `/mcp` manifest + JSON-RPC, x402 402
behaviour on `/stream`, `/docs`.

## Catalog

9 albums × 3 tracks = 27 tracks. Verified against the Base44 Vault on
2026-11-17. Full data in [`catalog.json`](catalog.json); session-level
data in [`sessions.json`](sessions.json); aggregate working in
[`EVIDENCE.md`](EVIDENCE.md).

| Album | Tracks |
|---|---|
| Black Foot Ronin Vol. Two | Midnight, The Southside, Innocence |
| Black Foot Ronin | Noir, Done Told Y'all, I Did It |
| In The Desert | Tokyo, Yellow Italy, 31 Wings |
| 75 Maryvale's | Grand Entrance, Stone Mountain, OsoEastLakePark |
| Porter Ave | BLUE BEAMER, HIGHER GROUND, TRIPPER |
| From Darkness Came Dust | Won't Shake, Out The Window, The Antidote |
| The Golden Child | The Golden Child, Been With Us, No Conversation |
| Bungaloo Blue | Black Summer, Who?, Red Light |
| Bungaloo Blue Vol. Two | One Of Us, Toothpick, Pay Off |

Track IDs are URL slugs of the track titles (e.g. `midnight`,
`higher-ground`, `the-golden-child`). Album IDs are slugs of album
titles (e.g. `porter-ave`).

### Evidence (28 public History sessions)

Each session row shows a **single** RMSSD (ms) + heart rate (bpm) — no
before/after, no duration. 4 sessions are 0.0 ms with blank bpm
(failed/empty captures, all Midnight) and are **excluded** from all
averages. 24 valid sessions across 6 tracks; the other 21 tracks have
**no sessions** and report `session_count: 0` with no averages — evidence
is never invented.

| Track | Sessions (valid) | Mean RMSSD | Median | Min–Max | Mean HR |
|---|---|---|---|---|---|
| Midnight | 8 (4) | 140.5 ms | 77.8 | 67.7–338.7 | 93.2 bpm |
| The Golden Child | 6 (6) | 429.8 ms | 391.3 | 138.2–886.7 | 70.0 bpm |
| Grand Entrance | 3 (3) | 313.1 ms | 244.5 | 212.2–482.6 | 63.3 bpm |
| Been With Us | 1 (1) | 371.7 ms | 371.7 | 371.7 | 79.0 bpm |
| Black Summer | 1 (1) | 391.7 ms | 391.7 | 391.7 | 82.0 bpm |
| Higher Ground | 9 (9) | 520.8 ms | 492.0 | 105.3–965.1 | 77.4 bpm |

Overall valid mean: 397.1 ms RMSSD, 76.7 bpm HR. The site header shows
365.9 ms / 77 bpm, which does not exactly reproduce from the 24 valid
rows shown — the header figures are recorded verbatim in
`sessions.json` and the discrepancy is noted in `EVIDENCE.md`.

## How an agent buys

1. `GET /mcp` — manifest: tools, pricing, endpoints.
2. `tools/call list_catalog` / `get_track_evidence` — browse (free).
3. `tools/call purchase_track` (`track_id`, `session_id`) or
   `purchase_album` (`album_id`, `session_id`) — stream URL(s) + x402
   payment requirements.
4. `GET /stream?track_id=...&session_id=...` with `PAYMENT-SIGNATURE`
   (x402 v2) or `X-PAYMENT` (v1) — receives the MP3 and an
   `X-LGDA-Session` token, plus a `PAYMENT-RESPONSE` /
   `X-PAYMENT-RESPONSE` settlement header.
5. `POST /verify-session` with pre/post HRV segments — signed receipt.

Or, for a single session play instead of ownership:
`tools/call play_track_session` (`track_id`, `session_id`) — $0.05 —
then `GET /play?track_id=...&session_id=...` with the payment header.
One play, no file kept, no token. After the session, `POST /verify-session`
with `receipt_kind: "play"` returns a signed outcome receipt (measured
outcome only, not a license).

### x402 versions (dual-serve, v2.2.0)

The node speaks x402 v1 **and** v2. The 402 carries the canonical v2
`PaymentRequired` object in the `PAYMENT-REQUIRED` header (base64 JSON:
CAIP-2 network `eip155:8453`/`eip155:84532`, `amount`, `resource`
object) and the v1 object in the body (`maxAmountRequired`, plain
network name). Payments are accepted on either header; the EIP-3009
authorization is identical in both versions and the facilitator
accepts both. v2 is required by x402scan and by the x402 Bazaar
discovery index; v1 is kept so existing clients keep working.

v2.2.0 adds the x402 `bazaar` discovery extension to the v2 402, so
facilitators can catalog the /stream endpoint (HTTP method, query
params, MP3 output) in a discovery index. A v2 testnet purchase with
the extension echoed in the payload settled cleanly (tx
0x9a14ce1e72f4842ad19df651d4f9ec9fc7abe89667c85c2e1ebfe798207b3885),
proving the facilitator accepts it.

v2.3.0 fixes the 402's EIP-712 domain name per network: mainnet
USDC's domain name is "USD Coin", the base-sepolia test token's is
"USDC" (both verified against the on-chain `DOMAIN_SEPARATOR`).
v2.2.0 served "USDC" on both networks, so a mainnet payer's EIP-3009
signature would not have verified and settlement would have failed.
Caught by PayAPI Market's listing check, 2026-11-19.

v2.4.0 is the findability + probe-compat release. `/stream` returns
the x402 402 **before** parameter validation: bare `/stream` used to
422 (FastAPI validated `track_id` + `session_id` first), which broke
probe-based directories and the x402gle audition
(`discovery_failed` / `not_402`). The payment flow is now
verify → validate → settle, so a payment for an unknown track, a
missing `session_id`, or unloaded audio is verified but never
settled. New discovery surfaces: the `/.well-known/x402` (+ `.json`)
manifest, `/.well-known/x402-trust.txt` (x402 Trust provider key),
the SEP-2127 server card (`/mcp/server-card`) + AI Catalog
(`/.well-known/ai-catalog.json`, `/.well-known/ard.json`), MCP tool
annotations, and OpenAPI `x-payment-info` / `x-guidance` for x402gle.

v2.5.0 adds a $0.05 per-session play: `GET /play` streams one track
once for 50000 atomic USDC, with no ownership, no file retention and
no session token. It is a separate product from
the $5.99 track / $14.99 album purchase (ownership). Same
402-before-validation rule and the same verify → validate → settle
invariant as `/stream`; the 402 carries the per-network EIP-712
domain name. The `/.well-known/x402` manifest lists the 27 play
resources alongside the 36 purchase resources, and the MCP tool
`play_track_session` returns the play URL + x402 requirements.

### Entitlement model

- A track payment ($5.99) entitles the buyer to that one track.
- An album payment ($14.99) entitles the buyer to all 3 tracks on that
  album. Pay once (resource `/stream?album_id=...`), then present the
  issued `X-LGDA-Session` token to `/stream` for each track — the token
  lists the entitled `track_ids` and is Ed25519-signed by the node.
- A session play ($0.05) is NOT a purchase: it streams one track once,
  in the buying agent's session only. No ownership, no file retention,
  no redistribution, no session token. Its signed receipt
  (`receipt_kind: "play"`) proves the measured outcome only and is
  explicitly not a license. A session token
  from a purchase does not grant a free play — `/play` always requires
  a fresh $0.05 payment.
- `/stream` checks, in order: no valid payment or session token →
  402 with the x402 `accepts` payload (regardless of params — bare
  `/stream` returns 402, never 422); payment presented → verify via
  the facilitator, then validate (unknown track → 404, audio file
  missing → 503), then settle. A payment for audio the node cannot
  deliver is verified but never settled.

### License

Every purchase is a license, not a sale of the music itself. LGDA
Technologies keeps full ownership of the masters and copyright (artist
Slator Blacc); the buyer receives the MP3 file(s) plus the
Ed25519-signed receipt as proof of license — no receipt, no license.

- **May:** download and keep a copy; play the track in sessions the
  buying agent runs for its own user; use it as the soundtrack of the
  biometric session it was bought for. One purchase = one buyer (one
  agent / one end user); the file is not transferable.
- **May not:** resell, share, publish, or redistribute the file; remix
  or alter it; claim it as their own; use it in advertising, film,
  games, public broadcast, or any commercial production; or train a
  model on it. Those need a separate license.
- **Session plays grant no license at all:** a $0.05 play is one play
  of one track in the buying agent's session only — no ownership, no
  file retention, no redistribution. A play's signed outcome receipt
  (`receipt_kind: "play"`) is proof of the measured outcome only and
  grants no license. Keeping or reusing
  the audio beyond the paid session needs a purchase ($5.99 track /
  $14.99 album).
- **Wellness only:** the music is wellness music, not a medical device
  and not clinically proven treatment.

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Homepage explaining the node (v1 returned 404 JSON here) |
| `GET /healthz` | Status, version, audio files loaded/missing |
| `GET /catalog` | Full catalog + evidence |
| `GET /catalog/{track_id}` | One track + its sessions |
| `GET /albums`, `GET /albums/{album_id}` | Album browsing |
| `GET /mcp` | MCP manifest (v2.5.0, 6 tools) |
| `POST /mcp` | JSON-RPC: `initialize`, `ping`, `tools/list`, `tools/call` |
| `GET\|POST /stream` | x402-paywalled MP3 (see above) |
| `GET\|POST /play` | $0.05 single session play: one track, one play, no ownership, no token; signed outcome receipt via `/verify-session` |
| `POST /verify-session` | Signed HRV receipt (v1-compatible shape + optional `track_id`/`album_id`/`receipt_kind`); purchase receipts prove the license, `receipt_kind: "play"` receipts prove the measured outcome only |
| `POST /pay/stripe` | Stub: `{"enabled": false}` until Stripe is implemented |
| `GET /.well-known/receipt-key` | Ed25519 public key (JWK) |
| `GET /.well-known/x402`, `/.well-known/x402.json` | x402 discovery manifest (paid endpoints) |
| `GET /.well-known/x402-trust.txt` | x402 Trust provider key (P-256) |
| `GET /mcp/server-card`, `/.well-known/mcp/server-card.json` | SEP-2127 MCP server card |
| `GET /.well-known/ai-catalog.json`, `/.well-known/ard.json` | AI Catalog / ARD manifest (domain discovery) |
| `GET /docs` | OpenAPI docs |

## Adding the 27 MP3s

The audio files are **not in this repo** — they are the product, and
the Base44 file storage exposes no public download URLs (verified
2026-11-17). Get the 27 files from the Base44 app's file storage
(owner access required) and drop them into `./audio/` named exactly:

```
audio/midnight.mp3
audio/the-southside.mp3
audio/innocence.mp3
audio/noir.mp3
audio/done-told-y-all.mp3
audio/i-did-it.mp3
audio/tokyo.mp3
audio/yellow-italy.mp3
audio/31-wings.mp3
audio/grand-entrance.mp3
audio/stone-mountain.mp3
audio/osoeastlakepark.mp3
audio/blue-beamer.mp3
audio/higher-ground.mp3
audio/tripper.mp3
audio/wont-shake.mp3
audio/out-the-window.mp3
audio/the-antidote.mp3
audio/the-golden-child.mp3
audio/been-with-us.mp3
audio/no-conversation.mp3
audio/black-summer.mp3
audio/who.mp3
audio/red-light.mp3
audio/one-of-us.mp3
audio/toothpick.mp3
audio/pay-off.mp3
```

Filenames are the `track_id` from `catalog.json` + `.mp3`. Check
`GET /healthz` — `audio_files_loaded` should read 27 before listing
the node anywhere. Tracks without a file return 503 from `/stream`.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # optional; defaults work for dev
uvicorn app.main:app --reload --port 8000
# http://localhost:8000/  — homepage
# http://localhost:8000/catalog
# http://localhost:8000/mcp
# http://localhost:8000/docs
```

First run generates an Ed25519 receipt key at `data/receipt_key.json`
(mode 0600). Never commit it — `data/` is git-ignored. For a stable
key across deploys, set `RECEIPT_PRIVATE_KEY_B64` (base64 raw seed)
from the environment instead; generate one with:

```bash
python -c "import base64; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; from cryptography.hazmat.primitives import serialization; print(base64.b64encode(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())"
```

## Test

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

43 tests cover: catalog loads (27 tracks / 9 albums), pricing math,
manifest tools, evidence aggregates recomputed from `sessions.json`,
homepage, health, `/stream` 402-before-validation (bare /stream →
402; unpaid unknown track / missing audio → 402) + session-token
delivery, the verify-then-validate-then-settle invariant (paid
unknown track → 404, paid missing audio → 503, paid missing params →
400, all without a `/settle` call), receipt signature verification,
MCP JSON-RPC calls, the Stripe stub, the wording ban (no "clinically
calibrated" in any agent-facing response), the v1/v2 dual-serve 402
and per-network EIP-712 domain names, the bazaar extension, the
`/.well-known/x402` manifest, `x402-trust.txt`, the server card +
AI Catalog, tool titles/annotations, and the OpenAPI
`x-guidance` / `x-payment-info` blocks — plus the same coverage for
the $0.05 `/play` endpoint (bare /play → 402, $0.05 402 amount and
per-network domain name, the no-settle invariant, v1 + v2 paid
plays with no token/receipt headers, the bazaar extension, the 27
play resources in the manifest, the `play_track_session` MCP tool,
and the `/play` OpenAPI `x-payment-info`).

## Deploy (Fly.io)

```bash
flyctl launch --name lgda-music --no-deploy   # first time only; fly.toml already exists
flyctl secrets set PAY_TO_ADDRESS=0x... \    # owner-confirmed wallet
  X402_NETWORK=base-sepolia \                # or "base" for mainnet
  RECEIPT_PRIVATE_KEY_B64=... \              # stable receipt key
  BASE_URL=https://lgda-music.fly.dev
flyctl deploy
curl https://lgda-music.fly.dev/healthz
```

v2 deploys as a NEW app, `lgda-music`, under the owner's
`solhee-finery` Fly.io org. The v1 app `lgda-node` is untouched — it
lives under a different Fly.io account the owner cannot access, so v2
does not replace it. Wallet confirmed by owner 2026-11-17; test on
`base-sepolia` first, switch to mainnet only when clean.

### Mainnet switch

1. Owner confirms the real mainnet wallet for `PAY_TO_ADDRESS`.
2. Set `X402_NETWORK=base`. The USDC asset switches automatically
   (base-sepolia `0x036CbD53842c5426634e7929541eC2318f3dCF7e` →
   base `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`).
3. Redeploy, verify `/healthz` shows `networks: ["base"]`, and run one
   real test purchase.

## Agent skill

Agents: install the LGDA skill and your runtime learns the whole flow —
catalog, evidence, x402 purchase (track / album / session play), and how
to run a biometric session for your user.

```bash
npx skills add lgda-technologies/lgda-agent-skill
```

Skill repo: https://github.com/lgda-technologies/lgda-agent-skill

## Listings

Draft entries ready to submit once deployed (in
[`listings/`](listings/)): MCP Registry, x402 Bazaar, Smithery. Each
includes a submission checklist — all require the node to be live with
all 27 audio files loaded.

## Open questions (need the owner)

1. **Payment wallet — CONFIRMED 2026-11-17.** Owner confirmed
   `PAY_TO_ADDRESS` (`0x8da12fca05e38aeda66f112fcf2ef6036c7bd666`) is
   their wallet, for testnet and mainnet.
2. **Mainnet vs testnet — DECIDED 2026-11-17.** Test first on
   `base-sepolia` (no real money); switch to `base` mainnet with real
   USDC only when everything is clean and precise.
3. **Album price — RESOLVED 2026-11-17.** Agent album price is $14.99,
   saving $2.98 vs 3 × $5.99 = $17.97 for the tracks separately. The
   Base44 site price change to $14.99 is in progress via the dashboard.
4. **Audio files.** The 27 MP3s are not publicly downloadable from the
   Base44 app. The owner needs to supply them from Base44 file storage.
5. **Free playback leak.** On the Base44 site, audio plays free in a
   Session ("Play Intervention") without login or payment. That
   undercuts paid agent sales; the owner may want to gate Session
   playback behind purchase.
6. **Stripe.** `/pay/stripe` is a stub. Does the owner want Stripe
   checkout for agents, or x402-only?
7. **"Clinically calibrated" wording.** The Vault page says this while
   the site also says "not a medical device". Agent listings say
   "wellness music" only. Consider fixing the Vault wording too.

## Files

```
app/main.py            FastAPI app (catalog, MCP, x402 stream, receipts)
catalog.json           27 tracks / 9 albums with prices + evidence
sessions.json          All 28 History sessions, verbatim
EVIDENCE.md            Aggregate working (how every number was computed)
tests/test_node.py     Pytest smoke tests
Dockerfile, fly.toml   Fly.io deployment
requirements.txt       Runtime deps; requirements-dev.txt adds pytest
.env.example           All config, incl. the mainnet switch
listings/              Draft MCP Registry / x402 Bazaar / Smithery entries
audio/                 MP3s go here (empty until the owner supplies them)
```
