#!/usr/bin/env python3
"""Ingest the 24 retrieved Base44 audio files into audio/{track_id}.mp3.

Sources: ~/workspace/browser_downloads/sess-3111351068/ (masters, never modified).
- .mp3 files are copied as-is.
- .wav files are converted to 320 kbps MP3 (libmp3lame) for delivery; the
  WAV masters stay in the downloads dir.

3 tracks had NO audio file in Base44 at all (From Darkness Came Dust:
i-am-alive, t-r-u-u-p-s, tonight-we-dance) — verified 2026-11-17 by opening
each Track record; audio_url is empty. The owner replaced that album the
same day with three tracks exported from Untitled (Won't Shake,
Out The Window, The Antidote — see browser_downloads/sess-3999682745/);
they were ingested directly (two MP3 copies + one WAV→320k MP3), so the
catalog is now 27/27 with no missing audio. The original three tracks will
return later as From Darkness Came Dust Vol. Two.

Usage: python3 scripts/ingest_audio.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path.home() / "workspace/browser_downloads/sess-3111351068"
DEST = ROOT / "audio"

# distinctive filename fragment -> track_id
MP3 = {
    "INNOCENCE__final_": "innocence",
    "The_Southside__Prod_Treytice_": "the-southside",
    "I_Did_it_Final": "i-did-it",
    "Done_Told_Y_all": "done-told-y-all",
    "Noir_Final": "noir",
    "HIGHER_GROUND_DRAFT__2": "higher-ground",
    "AWAY_DRAFT_": "blue-beamer",
    "Colors_2ndBounce": "no-conversation",
    "Been_With_Us_-__32123_": "been-with-us",
    "The_Golden_Child_-__3623__2": "the-golden-child",
}
WAV = {
    "Midnight_Final": "midnight",
    "31_WINGS__Rough_Cut_": "31-wings",
    "Who_Really_Be": "yellow-italy",
    "Japan": "tokyo",
    "TRIPPER_": "tripper",
    "Stone_Mountain": "stone-mountain",
    "OsoEastLakePark": "osoeastlakepark",
    "Grand_Entrance": "grand-entrance",
    "pay_off": "pay-off",
    "toothpick": "toothpick",
    "one_of_us": "one-of-us",
    "red_light": "red-light",
    "who-": "who",
    "black_summer": "black-summer",
}


def find_src(fragment: str, ext: str) -> Path:
    matches = list(SRC.glob(f"*{fragment}*.{ext}"))
    if len(matches) != 1:
        raise SystemExit(f"expected 1 match for {fragment!r}.{ext}, got {len(matches)}: {matches}")
    return matches[0]


def main() -> None:
    DEST.mkdir(exist_ok=True)
    done, failed = [], []
    for fragment, track_id in MP3.items():
        src = find_src(fragment, "mp3")
        dst = DEST / f"{track_id}.mp3"
        shutil.copyfile(src, dst)
        done.append((track_id, "copy", dst.stat().st_size))
    for fragment, track_id in WAV.items():
        src = find_src(fragment, "wav")
        dst = DEST / f"{track_id}.mp3"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-codec:a", "libmp3lame", "-b:a", "320k", str(dst)],
            capture_output=True, text=True)
        if r.returncode != 0:
            failed.append((track_id, r.stderr.strip()[-300:]))
        else:
            done.append((track_id, "wav->mp3", dst.stat().st_size))
    for track_id, how, size in sorted(done):
        print(f"OK   {track_id:20s} {how:9s} {size/1e6:6.1f} MB")
    for track_id, err in failed:
        print(f"FAIL {track_id}: {err}")
    print(f"\n{len(done)} ingested, {len(failed)} failed, "
          f"total {sum(s for _, _, s in done)/1e6:.1f} MB")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
