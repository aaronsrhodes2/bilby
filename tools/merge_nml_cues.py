#!/usr/bin/env python3
"""
merge_nml_cues.py — Merge cue points from PC NML into a Mac-enriched NML.

Use case: Mac updates collection.nml with album art / metadata enrichment,
then pushes to git. PC pulls, runs this script to transplant its cue points
(CUE_V2 elements) into the Mac's version, then pushes the merged result.

Strategy:
  - Mac NML  = "theirs" = authoritative for metadata (art, BPM, key, album info)
  - PC NML   = "ours"   = authoritative for cue points (CUE_V2 elements)
  - Merge    = Mac's metadata + PC's cues, matched by normalised artist+title key

Usage:
  py -3.13 tools/merge_nml_cues.py --report
  py -3.13 tools/merge_nml_cues.py --merge
  py -3.13 tools/merge_nml_cues.py --merge --mac-nml path/to/mac.nml --pc-nml path/to/pc.nml
"""

from __future__ import annotations
import argparse, re, shutil, sys
from pathlib import Path
import xml.etree.ElementTree as ET

# ── Default paths ──────────────────────────────────────────────────────────────
REPO        = Path(__file__).resolve().parent.parent
WORKTREE    = REPO / ".claude/worktrees/upbeat-haslett-77de35"
# Mac's NML comes in via git pull into the main repo
MAC_NML_DEF = REPO / "corrected_traktor" / "collection.nml"
# PC's cue-enriched NML lives in the worktree
PC_NML_DEF  = WORKTREE / "corrected_traktor" / "collection.nml"
OUT_NML_DEF = REPO / "corrected_traktor" / "collection.nml"

# ── Key normalisation (same logic as reset_cues.py / stage9_stt_pc.py) ─────────
_VER = re.compile(r'\s*[\(\[][^)\]]*[\)\]]')

def dkey(artist: str, title: str) -> str:
    t = _VER.sub("", title or "").strip().lower()
    return f"{(artist or '').strip().lower()}\t{t}"

# ── Build cue index from PC NML ────────────────────────────────────────────────
def build_pc_cue_index(pc_nml: Path) -> dict[str, list[ET.Element]]:
    """Returns {dkey: [CUE_V2, CUE_V2, ...]} for all entries that have cues."""
    tree = ET.parse(str(pc_nml))
    coll = tree.getroot().find("COLLECTION")
    index: dict[str, list[ET.Element]] = {}
    for e in coll.findall("ENTRY"):
        cues = e.findall("CUE_V2")
        if not cues:
            continue
        k = dkey(e.get("ARTIST", ""), e.get("TITLE", ""))
        if k not in index:
            index[k] = cues
    return index

# ── Report ─────────────────────────────────────────────────────────────────────
def report(mac_nml: Path, pc_nml: Path) -> None:
    print(f"Mac NML : {mac_nml}  ({mac_nml.stat().st_size/1e6:.1f} MB)")
    print(f"PC NML  : {pc_nml}  ({pc_nml.stat().st_size/1e6:.1f} MB)")

    mac_tree = ET.parse(str(mac_nml))
    mac_coll = mac_tree.getroot().find("COLLECTION")
    mac_entries = mac_coll.findall("ENTRY")

    pc_index = build_pc_cue_index(pc_nml)

    mac_total   = len(mac_entries)
    mac_cued    = sum(1 for e in mac_entries if e.findall("CUE_V2"))
    pc_cued     = len(pc_index)
    will_gain   = 0
    will_update = 0
    no_match    = 0

    for e in mac_entries:
        k = dkey(e.get("ARTIST", ""), e.get("TITLE", ""))
        if k in pc_index:
            if e.findall("CUE_V2"):
                will_update += 1
            else:
                will_gain += 1
        else:
            no_match += 1

    print(f"\nMac NML entries      : {mac_total:,}")
    print(f"  Already have cues  : {mac_cued:,}")
    print(f"  No cues yet        : {mac_total - mac_cued:,}")
    print(f"PC cue index entries : {pc_cued:,}")
    print(f"\nAfter merge:")
    print(f"  Will gain cues     : {will_gain:,}  (Mac entry had none, PC has them)")
    print(f"  Will update cues   : {will_update:,}  (both have cues — PC wins)")
    print(f"  No PC match        : {no_match:,}  (new Mac tracks not in PC NML)")

# ── Merge ──────────────────────────────────────────────────────────────────────
def merge(mac_nml: Path, pc_nml: Path, out_nml: Path) -> None:
    print(f"Loading Mac NML  ({mac_nml.stat().st_size/1e6:.1f} MB)...")
    mac_tree = ET.parse(str(mac_nml))
    mac_root = mac_tree.getroot()
    mac_coll = mac_root.find("COLLECTION")

    print(f"Loading PC cue index...")
    pc_index = build_pc_cue_index(pc_nml)
    print(f"  {len(pc_index):,} entries with cue points")

    gained = updated = skipped = 0

    for e in mac_coll.findall("ENTRY"):
        k = dkey(e.get("ARTIST", ""), e.get("TITLE", ""))
        pc_cues = pc_index.get(k)
        if pc_cues is None:
            skipped += 1
            continue

        # Remove existing CUE_V2 from Mac entry, replace with PC's
        for old in e.findall("CUE_V2"):
            e.remove(old)

        had_cues = len(e.findall("CUE_V2")) > 0  # always 0 after removal above
        for cue in pc_cues:
            e.append(cue)

        if had_cues:
            updated += 1
        else:
            gained += 1

    print(f"\nMerge results:")
    print(f"  Gained cues : {gained:,}")
    print(f"  Updated cues: {updated:,}")
    print(f"  No PC match : {skipped:,}")

    # Backup existing output if it exists
    if out_nml.exists() and out_nml != mac_nml:
        bak = out_nml.with_suffix(".nml.merge_bak")
        shutil.copy2(str(out_nml), str(bak))
        print(f"\nBacked up existing NML to {bak.name}")

    print(f"Writing merged NML to {out_nml}...")
    ET.indent(mac_tree, space="\t")
    mac_tree.write(str(out_nml), encoding="utf-8", xml_declaration=True)
    print(f"Done. ({out_nml.stat().st_size/1e6:.1f} MB)")
    print(f"\nNext steps:")
    print(f"  git add -f corrected_traktor/collection.nml")
    print(f"  git commit -m 'nml: merge Mac art/metadata + PC cue points'")
    print(f"  git push")

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Merge PC cue points into Mac-enriched NML")
    ap.add_argument("--report", action="store_true", help="Show what would change")
    ap.add_argument("--merge",  action="store_true", help="Run the merge and write output")
    ap.add_argument("--mac-nml", type=Path, default=MAC_NML_DEF,
                    help=f"Mac's NML (metadata source, default: {MAC_NML_DEF})")
    ap.add_argument("--pc-nml",  type=Path, default=PC_NML_DEF,
                    help=f"PC's NML (cue source, default: {PC_NML_DEF})")
    ap.add_argument("--out-nml", type=Path, default=OUT_NML_DEF,
                    help=f"Output path (default: overwrites mac-nml)")
    args = ap.parse_args()

    if not args.mac_nml.exists():
        print(f"ERROR: Mac NML not found: {args.mac_nml}", file=sys.stderr)
        sys.exit(1)
    if not args.pc_nml.exists():
        print(f"ERROR: PC NML not found: {args.pc_nml}", file=sys.stderr)
        sys.exit(1)

    if args.report:
        report(args.mac_nml, args.pc_nml)
    elif args.merge:
        merge(args.mac_nml, args.pc_nml, args.out_nml)
    else:
        ap.print_help()
