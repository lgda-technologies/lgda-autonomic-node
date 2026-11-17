# Evidence aggregates — working

Source: 28 public sessions from https://lgda-technologies.base44.app/history, audited 2026-11-17.
Each session row shows a SINGLE RMSSD (ms) and heart rate (bpm) — no before/after, no duration.
Rule: sessions with 0.0 ms / blank bpm are failed/empty captures and are EXCLUDED from all averages.
Valid sessions = rmssd_ms > 0. All aggregates below were computed in Python (statistics.mean/median).

Overall: 28 sessions total, 24 valid, 4 excluded (all Midnight, 0.0 ms).
Overall valid mean RMSSD = 397.1 ms; mean HR = 76.7 bpm.
Site header stats (as displayed, 90 days): avg RMSSD 365.9 ms, avg HR 77 bpm, 28 sessions.
Note: the site header (365.9 ms / 77 bpm) does not exactly reproduce from the 24 valid rows shown
(valid mean 397.1 ms / 76.7 bpm) — the header likely uses a different
window or includes/excludes rows differently. Per-track aggregates below use only the valid rows listed,
and the site header figures are recorded verbatim in sessions.json metadata for transparency.

- Midnight (midnight): 8 sessions, 4 valid; RMSSD values [72.8, 67.7, 82.7, 338.7] -> mean 140.5, median 77.8, min 67.7, max 338.7; HR [107, 65, 129, 72] -> mean 93.2
  Excluded 4 x 0.0 ms / blank bpm.
- The Southside (the-southside): 0 sessions — no evidence, no averages. Never invent.
- Innocence (innocence): 0 sessions — no evidence, no averages. Never invent.
- Noir (noir): 0 sessions — no evidence, no averages. Never invent.
- Done Told Y'all (done-told-y-all): 0 sessions — no evidence, no averages. Never invent.
- I Did It (i-did-it): 0 sessions — no evidence, no averages. Never invent.
- Tokyo (tokyo): 0 sessions — no evidence, no averages. Never invent.
- Yellow Italy (yellow-italy): 0 sessions — no evidence, no averages. Never invent.
- 31 Wings (31-wings): 0 sessions — no evidence, no averages. Never invent.
- Grand Entrance (grand-entrance): 3 sessions, 3 valid; RMSSD values [482.6, 212.2, 244.5] -> mean 313.1, median 244.5, min 212.2, max 482.6; HR [66, 80, 44] -> mean 63.3
- Stone Mountain (stone-mountain): 0 sessions — no evidence, no averages. Never invent.
- OsoEastLakePark (osoeastlakepark): 0 sessions — no evidence, no averages. Never invent.
- BLUE BEAMER (blue-beamer): 0 sessions — no evidence, no averages. Never invent.
- HIGHER GROUND (higher-ground): 9 sessions, 9 valid; RMSSD values [541.9, 105.3, 965.1, 600.6, 400.6, 492.0, 489.1, 709.8, 383.0] -> mean 520.8, median 492.0, min 105.3, max 965.1; HR [91, 78, 66, 93, 84, 87, 56, 75, 67] -> mean 77.4
- TRIPPER (tripper): 0 sessions — no evidence, no averages. Never invent.
- Won't Shake (wont-shake): 0 sessions — no evidence, no averages. Never invent.
- Out The Window (out-the-window): 0 sessions — no evidence, no averages. Never invent.
- The Antidote (the-antidote): 0 sessions — no evidence, no averages. Never invent.
- The Golden Child (the-golden-child): 6 sessions, 6 valid; RMSSD values [138.2, 237.5, 263.9, 518.7, 886.7, 533.9] -> mean 429.8, median 391.3, min 138.2, max 886.7; HR [57, 71, 85, 64, 65, 78] -> mean 70.0
- Been With Us (been-with-us): 1 sessions, 1 valid; RMSSD values [371.7] -> mean 371.7, median 371.7, min 371.7, max 371.7; HR [79] -> mean 79.0
- No Conversation (no-conversation): 0 sessions — no evidence, no averages. Never invent.
- Black Summer (black-summer): 1 sessions, 1 valid; RMSSD values [391.7] -> mean 391.7, median 391.7, min 391.7, max 391.7; HR [82] -> mean 82.0
- Who? (who): 0 sessions — no evidence, no averages. Never invent.
- Red Light (red-light): 0 sessions — no evidence, no averages. Never invent.
- One Of Us (one-of-us): 0 sessions — no evidence, no averages. Never invent.
- Toothpick (toothpick): 0 sessions — no evidence, no averages. Never invent.
- Pay Off (pay-off): 0 sessions — no evidence, no averages. Never invent.
