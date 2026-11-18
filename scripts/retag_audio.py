#!/usr/bin/env python3
"""Retag all 27 catalog MP3s with clean ID3 metadata.

Reads titles/album/track order from catalog.json (Base44 names).
Sets: artist + album artist = Slator Blacc, title, album, track N/3,
copyright + publisher = LGDA Technologies.
Strips all existing junk tags (draft titles, wrong genre, stale album
"BFR", "Jonah Garcia" artist) but KEEPS any embedded artwork (APIC).
Writes ID3v2.3 for maximum player compatibility.

Usage: python3 scripts/retag_audio.py
"""
import json
import sys
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCOP, TIT2, TPE1, TPE2, TPUB, TRCK

ROOT = Path(__file__).resolve().parent.parent
ARTIST = "Slator Blacc"
OWNER = "LGDA Technologies"


def main() -> int:
    catalog = json.loads((ROOT / "catalog.json").read_text())
    albums = {a["album_id"]: a for a in catalog["albums"]}
    tracks = {t["track_id"]: t for t in catalog["tracks"]}

    tagged = 0
    kept_art = 0
    for track_id, track in sorted(tracks.items()):
        album = albums[track["album_id"]]
        pos = album["track_ids"].index(track_id) + 1
        path = ROOT / "audio" / f"{track_id}.mp3"
        if not path.exists():
            print(f"MISSING: {path}")
            continue

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        # Keep embedded artwork, drop everything else.
        apic = [f for f in tags.values() if f.FrameID == "APIC"]
        if apic:
            kept_art += 1
        tags.clear()
        for frame in apic:
            tags.add(frame)

        tags.add(TIT2(encoding=3, text=track["title"]))
        tags.add(TPE1(encoding=3, text=ARTIST))
        tags.add(TPE2(encoding=3, text=ARTIST))
        tags.add(TALB(encoding=3, text=track["album"]))
        tags.add(TRCK(encoding=3, text=f"{pos}/{len(album['track_ids'])}"))
        tags.add(TCOP(encoding=3, text=OWNER))
        tags.add(TPUB(encoding=3, text=OWNER))
        tags.save(path, v2_version=3)
        tagged += 1
        print(f"{track_id}: {track['title']} / {track['album']} ({pos}/3)")

    print(f"\nTagged {tagged}/27 files; kept artwork on {kept_art} files.")
    return 0 if tagged == 27 else 1


if __name__ == "__main__":
    sys.exit(main())
