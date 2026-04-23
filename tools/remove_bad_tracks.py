#!/usr/bin/env python3
"""Remove bad/unplayable tracks from NML and lyrics_dedup.json."""
import json, re, xml.etree.ElementTree as ET
from pathlib import Path

REPO      = Path(r"D:\Aaron\development\music-collection")
NML       = REPO / "corrected_traktor/collection.nml"
DEDUP     = REPO / "state/lyrics_dedup.json"
BAD_JSON  = REPO / "state/stt_bad_files.json"

bad_files = json.loads(BAD_JSON.read_text(encoding="utf-8"))
print(f"Bad files to remove: {len(bad_files)}")
for p, info in bad_files.items():
    print(f"  {info['artist']} - {info['title']}")

_VER = re.compile(r'\s*[\(\[][^)\]]*[\)\]]')

def dkey(artist, title):
    t = _VER.sub("", title or "").strip().lower()
    return f"{(artist or '').strip().lower()}\t{t}"

bad_keys = set()
for p, info in bad_files.items():
    bad_keys.add(dkey(info["artist"], info["title"]))
print(f"\nBad dkeys: {bad_keys}")

# --- NML cleanup ---
print("\nParsing NML...")
tree = ET.parse(str(NML))
root = tree.getroot()
coll = root.find("COLLECTION")
entries = coll.findall("ENTRY")
print(f"  Entries before: {len(entries)}")

removed_nml = 0
for e in list(entries):
    k = dkey(e.get("ARTIST", ""), e.get("TITLE", ""))
    if k in bad_keys:
        coll.remove(e)
        removed_nml += 1
        print(f"  Removed NML entry: {e.get('ARTIST','')} - {e.get('TITLE','')}")

remaining = len(coll.findall("ENTRY"))
coll.set("ENTRIES", str(remaining))
print(f"  Entries after: {remaining}  (removed {removed_nml})")

ET.indent(tree, space="\t")
tree.write(str(NML), encoding="utf-8", xml_declaration=True)
print("  NML written.")

# --- lyrics_dedup.json cleanup ---
print("\nCleaning lyrics_dedup.json...")
dedup = json.loads(DEDUP.read_text(encoding="utf-8"))
before = len(dedup)
for k in list(dedup.keys()):
    if k in bad_keys:
        del dedup[k]
        print(f"  Removed dedup key: {k!r}")
after = len(dedup)
DEDUP.write_text(json.dumps(dedup, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  dedup entries: {before} -> {after}")

print("\nDone.")
