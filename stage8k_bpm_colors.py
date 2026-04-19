#!/usr/bin/env python3
"""
Stage 8k — BPM Color Stripes

Sets Traktor track stripe colors based on BPM, anchored on 118–124 BPM = Red.
Color encodes distance from the anchor, so you can spot compatible BPMs at a glance.

Color map (symmetric — same color above and below anchor):
  Red    (1) →  118–124  BPM  ← anchor
  Orange (2) →  112–117  or  125–130
  Yellow (3) →  106–111  or  131–136
  Green  (4) →  100–105  or  137–142
  Teal   (5) →   94–99   or  143–148
  Blue   (6) →   88–93   or  149–154
  Violet (7) →   <88     or   >154

Usage:
    python3 stage8k_bpm_colors.py            # dry-run: show distribution
    python3 stage8k_bpm_colors.py --apply    # write colors to both NMLs
"""

import argparse
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# ── Paths ─────────────────────────────────────────────────────────────────────

TRAKTOR_NML = (
    Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
)
OUR_NML = Path(__file__).parent / "corrected_traktor" / "collection.nml"

# ── Color map ─────────────────────────────────────────────────────────────────

# Traktor integer color codes (as stored in NML INFO COLOR attribute)
COLOR_RED    = "1"
COLOR_ORANGE = "2"
COLOR_YELLOW = "3"
COLOR_GREEN  = "4"
COLOR_TEAL   = "5"
COLOR_BLUE   = "6"
COLOR_VIOLET = "7"

COLOR_NAMES = {
    "1": "Red   ",
    "2": "Orange",
    "3": "Yellow",
    "4": "Green ",
    "5": "Teal  ",
    "6": "Blue  ",
    "7": "Violet",
}

# Anchor zone
ANCHOR_LO = 118
ANCHOR_HI = 124


def bpm_to_color(bpm: float) -> str:
    """Return Traktor color code string for a given BPM value."""
    b = round(bpm)
    if ANCHOR_LO <= b <= ANCHOR_HI:
        return COLOR_RED
    dist = min(abs(b - ANCHOR_LO), abs(b - ANCHOR_HI))
    if   dist <=  6: return COLOR_ORANGE   # 112–117 or 125–130
    elif dist <= 12: return COLOR_YELLOW   # 106–111 or 131–136
    elif dist <= 18: return COLOR_GREEN    # 100–105 or 137–142
    elif dist <= 24: return COLOR_TEAL     #  94–99  or 143–148
    elif dist <= 30: return COLOR_BLUE     #  88–93  or 149–154
    else:            return COLOR_VIOLET   # < 88    or  > 154


# ── Traktor process guard ─────────────────────────────────────────────────────

def traktor_is_running() -> bool:
    return subprocess.run(["pgrep", "-f", "Traktor"],
                          capture_output=True).returncode == 0


def quit_traktor_gracefully(timeout: int = 15) -> bool:
    subprocess.run(["osascript", "-e", 'tell application "Traktor 4" to quit'],
                   capture_output=True)
    for _ in range(timeout * 2):
        time.sleep(0.5)
        if not traktor_is_running():
            return True
    return not traktor_is_running()


def relaunch_traktor() -> None:
    subprocess.Popen(["open", "-a", "Traktor 4"])


def ensure_traktor_closed(apply: bool) -> bool:
    if not traktor_is_running():
        return True
    if not apply:
        print("[INFO] Traktor is running — dry-run is read-only, no problem.")
        return True
    print()
    print("⚠️  Traktor is currently running.")
    answer = input("Quit Traktor now so we can apply safely? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes"):
        print("   Sending graceful quit...", end=" ", flush=True)
        if quit_traktor_gracefully():
            print("done.")
            time.sleep(1)
            return True
        print("FAILED. Aborting.")
        return False
    print("Aborting. Close Traktor manually and re-run.")
    return False


# ── XML helpers ───────────────────────────────────────────────────────────────

def fix_xml_declaration(path: Path) -> None:
    content = path.read_bytes()
    content = content.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(content)


# ── Core logic ────────────────────────────────────────────────────────────────

def process_nml(nml_path: Path, apply: bool, label: str) -> Counter:
    ET.register_namespace("", "")
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")

    color_counter: Counter = Counter()
    no_bpm = 0

    for e in coll.findall("ENTRY"):
        info = e.find("INFO")
        if info is None:
            continue
        tempo = e.find("TEMPO")
        if tempo is None or not tempo.get("BPM"):
            no_bpm += 1
            continue

        bpm   = float(tempo.get("BPM"))
        color = bpm_to_color(bpm)
        color_counter[color] += 1

        if apply:
            info.set("COLOR", color)

    if apply:
        stamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = nml_path.parent / f"{nml_path.stem}_pre_bpm_colors_{stamp}.nml"
        shutil.copy2(nml_path, backup)
        tree.write(str(nml_path), encoding="UTF-8", xml_declaration=True)
        fix_xml_declaration(nml_path)
        print(f"  [{label}] Written → {nml_path}")
        print(f"  [{label}] Backup  → {backup.name}")

    return color_counter, no_bpm


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Color-code tracks by BPM")
    parser.add_argument("--apply",       action="store_true",
                        help="Write colors to NML files (default: dry-run)")
    parser.add_argument("--no-relaunch", action="store_true",
                        help="Do not offer to relaunch Traktor after applying")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"Stage 8k — BPM Color Stripes [{mode}]")
    print(f"  Anchor: {ANCHOR_LO}–{ANCHOR_HI} BPM = Red")
    print(f"  6-BPM rainbow blocks radiating outward\n")

    if not ensure_traktor_closed(args.apply):
        sys.exit(1)

    # Process both NMLs
    results = {}
    for nml_path, label in [(TRAKTOR_NML, "Traktor NML"), (OUR_NML, "Our NML  ")]:
        if not nml_path.exists():
            print(f"  [{label}] NOT FOUND — skipping")
            continue
        counter, no_bpm = process_nml(nml_path, args.apply, label)
        results[label] = (counter, no_bpm)

    # Report (use Traktor NML as reference)
    ref_label = "Traktor NML" if "Traktor NML" in results else next(iter(results))
    counter, no_bpm = results[ref_label]
    total = sum(counter.values())

    print(f"\n{'─'*55}")
    print(f"BPM color distribution ({total} tracks with BPM, {no_bpm} without):\n")
    print(f"  {'Color':<10}  {'Code'}  {'Count':>6}  {'%':>5}  BPM Range")
    print(f"  {'─'*10}  {'─'*4}  {'─'*6}  {'─'*5}  {'─'*22}")
    ranges = {
        "1": f"{ANCHOR_LO}–{ANCHOR_HI} BPM  ← anchor",
        "2": f"112–117  or  125–130",
        "3": f"106–111  or  131–136",
        "4": f"100–105  or  137–142",
        "5": f" 94–99   or  143–148",
        "6": f" 88–93   or  149–154",
        "7": f" <88     or   >154  ",
    }
    for code in ["1", "2", "3", "4", "5", "6", "7"]:
        count = counter.get(code, 0)
        pct   = count / total * 100 if total else 0
        bar   = COLOR_NAMES[code]
        print(f"  {bar}  [{code}]   {count:>6}  {pct:>4.1f}%  {ranges[code]}")

    if not args.apply:
        print(f"\nDry-run complete. Run with --apply to write colors.")
    else:
        print(f"\nStage 8k complete — {total} tracks colored.")
        if not args.no_relaunch:
            answer = input("\nRelaunch Traktor now? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                relaunch_traktor()
                print("Traktor relaunched.")


if __name__ == "__main__":
    main()
