#!/usr/bin/env python3
"""
traktor_sanitize.py
===================
Produces a clean, curated copy of a Traktor Pro library.

    python3 traktor_sanitize.py          # dry-run — shows what would happen
    python3 traktor_sanitize.py --apply  # execute

No accounts, no API keys, no internet connection required.
One external dependency: ffmpeg  (brew install ffmpeg)

Reads:  ~/Documents/Native Instruments/Traktor */collection.nml  (newest version,
        auto-detected — must be a library whose audio files still exist on disk)
Writes: curated_music/     audio files, organized by Artist/Album
        curated_traktor/   collection.nml pointing at curated_music/

No API calls. No credentials needed. Works completely offline.
Only external dependency: ffmpeg  (brew install ffmpeg)

What it does, in order:
  1. Load Traktor's collection.nml
  2. Drop clips shorter than 30 s (intros, snippets)
  3. Drop recordings longer than 12 minutes (live sets, DJ mixes)
  4. Drop live recordings (keyword detection in title / filename)
  5. Deduplicate by audio content hash — keep the copy with more metadata
  6. Normalize artist names:
       • strip feat./featuring/ft. suffixes
       • strip decade prefixes  (80's Pop Band → Pop Band)
       • apply title-case (preserves short ALL-CAPS acronyms like EBM, AC/DC)
       • merge "The X" / "X" variants — whichever has more tracks wins
  7. Recover artist from "Artist - Title" filename patterns
  8. Normalize genre tags to a 25-genre canonical taxonomy;
     fall back to most common genre for that artist in the collection
  9. Convert .wav files → MP3 LAME V0 (~220-260 kbps VBR) via ffmpeg
 10. Copy surviving files to curated_music/{Artist}/{Album}/{filename}
 11. Write curated_traktor/collection.nml with updated paths
"""

import argparse
import collections
import hashlib
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────

MIN_PLAYTIME = 30     # seconds — below this: intro / clip
MAX_PLAYTIME = 720    # seconds — above this: live set / DJ mix / compilation

# ─────────────────────────────────────────────────────────────────────────────
# Genre taxonomy and mapping
# (string-level only — no artist or track names appear here)
# ─────────────────────────────────────────────────────────────────────────────

GENRE_TAXONOMY = [
    "Deathrock", "Gothic Rock", "Darkwave", "Coldwave", "Post-Punk",
    "EBM", "Industrial", "New Wave", "Synthpop", "Ambient", "IDM",
    "Electronic", "Alternative Rock", "Indie Rock", "Punk", "Metal",
    "Hard Rock", "Classic Rock", "Rock", "Folk", "Pop", "Hip-Hop",
    "Soundtrack", "Comedy", "Other",
]

# Keys are lower-cased before lookup. Values are canonical genre names.
GENRE_MAP: dict[str, str] = {
    # EBM / Industrial
    "ebm": "EBM",  "e.b.m.": "EBM",  "electronic body music": "EBM",
    "future pop": "EBM",  "futurepop": "EBM",  "harsh ebm": "EBM",
    "dark electro": "EBM",  "electro-industrial": "Industrial",
    "industrial": "Industrial",  "industrial rock": "Industrial",
    "industrial metal": "Industrial",  "industrial dance": "Industrial",
    "power noise": "Industrial",  "aggrotech": "Industrial",
    "hellektro": "Industrial",  "noise": "Industrial",
    "power electronics": "Industrial",
    # Gothic / Darkwave
    "gothic rock": "Gothic Rock",  "goth rock": "Gothic Rock",  "gothic": "Gothic Rock",
    "deathrock": "Deathrock",  "death rock": "Deathrock",
    "darkwave": "Darkwave",  "dark wave": "Darkwave",
    "coldwave": "Coldwave",  "cold wave": "Coldwave",
    "post-punk": "Post-Punk",  "post punk": "Post-Punk",
    # Synth / Electronic
    "synthpop": "Synthpop",  "synth-pop": "Synthpop",  "synth pop": "Synthpop",
    "electropop": "Synthpop",  "new romantic": "New Wave",
    "new wave": "New Wave",  "nw/rock": "New Wave",
    "retrowave": "Electronic",  "synthwave": "Electronic",  "outrun": "Electronic",
    "darksynth": "Electronic",  "electronic": "Electronic",  "electronica": "Electronic",
    "dance": "Electronic",  "edm": "Electronic",  "techno": "Electronic",
    "trance": "Electronic",  "house": "Electronic",  "trip-hop": "Electronic",
    "downtempo": "Ambient",  "ambient house": "Ambient",
    "drum and bass": "Electronic",  "drum & bass": "Electronic",  "dnb": "Electronic",
    "breakbeat": "Electronic",  "big beat": "Electronic",
    "idm": "IDM",  "intelligent dance music": "IDM",
    "dark ambient": "Ambient",  "darkambient": "Ambient",
    # Ambient / New Age
    "ambient": "Ambient",  "drone": "Ambient",  "neoclassical": "Ambient",
    "new age": "Ambient",  "space music": "Ambient",
    # Rock
    "alternative rock": "Alternative Rock",  "alternative": "Alternative Rock",
    "indie rock": "Indie Rock",  "indie": "Indie Rock",
    "punk": "Punk",  "punk rock": "Punk",  "hardcore punk": "Punk",
    "metal": "Metal",  "heavy metal": "Metal",  "thrash metal": "Metal",
    "death metal": "Metal",  "black metal": "Metal",  "doom metal": "Metal",
    "power metal": "Metal",  "progressive metal": "Metal",
    "hard rock": "Hard Rock",  "classic rock": "Classic Rock",
    "rock": "Rock",  "pop rock": "Rock",  "psychedelic rock": "Rock",
    "progressive rock": "Rock",  "art rock": "Rock",
    "j-rock": "Rock",  "visual kei": "Rock",
    "alternative & punk": "Punk",  "alternative dance": "Alternative Rock",
    "synth rock": "Alternative Rock",
    # Pop / Other
    "pop": "Pop",  "dance pop": "Pop",  "teen pop": "Pop",  "j-pop": "Pop",
    "hip-hop": "Hip-Hop",  "hip hop": "Hip-Hop",  "rap": "Hip-Hop",
    "folk": "Folk",  "singer-songwriter": "Folk",  "acoustic": "Folk",
    "country": "Folk",  "blues": "Rock",  "r&b": "Pop",
    "soundtrack": "Soundtrack",  "score": "Soundtrack",  "film score": "Soundtrack",
    "comedy": "Comedy",  "novelty": "Comedy",
    "other": "Other",  "miscellaneous": "Other",  "various": "Other",
    "unclassifiable": "Other",
}

# ─────────────────────────────────────────────────────────────────────────────
# Regexes
# ─────────────────────────────────────────────────────────────────────────────

# Live recording indicators — must appear in a structural context
# to avoid false-positives ("I Will Live Again", "Live Wire", etc.)
LIVE_RE = re.compile(
    r"(\(live\b"                               # (live
    r"|\[live\b"                               # [live
    r"|\blive\s+at\b|\blive\s+in\b"           # "live at / live in"
    r"|\blive\s+from\b|\blive\s*-"            # "live from / live -"
    r"|-\s*live\b"                             # "- live"
    r"|\(unplugged\b|\[unplugged\b"            # (unplugged / [unplugged
    r"|\bunplugged\s+version\b"               # "unplugged version"
    r"|\bbbc\s+(?:radio|session)\b"           # BBC Radio / BBC Session
    r"|\blive\s+audio\b|\blive\s+recording\b" # "live audio / live recording"
    r"|\blive\s+version\b|\blive\s+session\b" # "live version / live session"
    r"|\blive\s+performance\b)",              # "live performance"
    re.IGNORECASE,
)

# Strip featured-artist suffix from artist field
FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring|with)\s+.+?[\)\]]?\s*$",
    re.IGNORECASE,
)

# Strip decade prefix: "80's Pop Band" → "Pop Band"
DECADE_RE = re.compile(r"^\d0'?s\s+", re.IGNORECASE)

# Split "Artist - Title" or "Title - Artist" patterns in filenames
DASH_RE = re.compile(r"^(.+?)\s+-\s+(.+)$")

# ─────────────────────────────────────────────────────────────────────────────
# NML path encoding  (Traktor stores paths with /: separators)
# ─────────────────────────────────────────────────────────────────────────────

def traktor_to_abs(volume: str, dir_: str, file_: str) -> str:
    """
    Decode Traktor LOCATION attributes → absolute path.
    VOLUME is ignored: the DIR field already encodes the full path from root
    using /: as a separator (e.g. '/:Users/:aaronrhodes/:Music/:').
    """
    s = dir_.strip()
    if s.startswith("/:"):
        s = s[2:]
    if s.endswith("/:"):
        s = s[:-2]
    parts = s.split("/:") if s else []
    return ("/" + "/".join(parts) + "/" + file_) if parts else ("/" + file_)


def abs_to_traktor_location(abs_path: str) -> dict[str, str]:
    """
    Encode an absolute path back into Traktor LOCATION attributes.
    VOLUME is left empty — Traktor resolves that itself on load.
    """
    p = Path(abs_path)
    # Build /: separated directory string from path components (skip root '/')
    dir_parts = list(p.parts[1:-1])   # everything between '/' and filename
    if dir_parts:
        dir_str = "/" + "/".join(f":{part}" for part in dir_parts) + "/:"
    else:
        dir_str = "/:"
    return {"VOLUME": "", "DIR": dir_str, "FILE": p.name}

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def find_source_nml() -> Path:
    """Auto-detect the newest Traktor collection.nml on this machine."""
    ni = Path.home() / "Documents" / "Native Instruments"
    candidates = sorted(
        (p for p in ni.glob("Traktor*/collection.nml")
         if "BACKUP" not in str(p.parent).upper()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise FileNotFoundError(
        "Could not find a Traktor collection.nml under:\n"
        f"  {ni}\n"
        "Make sure Traktor is installed and has been opened at least once."
    )


def file_md5(path: str, chunk_size: int = 1 << 20) -> str:
    """MD5 digest of file contents — used for duplicate detection."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while block := f.read(chunk_size):
            h.update(block)
    return h.hexdigest()


def safe_name(s: str, max_len: int = 80) -> str:
    """Sanitize a string for use as a filesystem directory name."""
    s = unicodedata.normalize("NFC", s or "")
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = re.sub(r"[\s_]+", " ", s).strip(" .")
    return s[:max_len] or "Unknown"


def fix_xml_declaration(path: Path) -> None:
    """Rewrite Python's single-quote XML declaration to Traktor's expected form."""
    data = path.read_bytes()
    data = data.replace(
        b"<?xml version='1.0' encoding='UTF-8'?>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>',
        1,
    )
    path.write_bytes(data)


def entry_playtime(entry: ET.Element) -> int:
    info = entry.find("INFO")
    if info is None:
        return 0
    try:
        return int(info.get("PLAYTIME", 0))
    except ValueError:
        return 0


def entry_location(entry: ET.Element) -> tuple[str, str, str]:
    loc = entry.find("LOCATION")
    if loc is None:
        return "", "", ""
    return loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")


def entry_abs_path(entry: ET.Element) -> str:
    return traktor_to_abs(*entry_location(entry))


def entry_album(entry: ET.Element) -> str:
    alb = entry.find("ALBUM")
    if alb is not None:
        return alb.get("TITLE", "")
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# Artist normalization
# ─────────────────────────────────────────────────────────────────────────────

_SMALL = {
    "a", "an", "the", "and", "but", "or", "for", "nor",
    "as", "at", "by", "in", "of", "on", "to", "up", "via",
    "vs", "vs.", "feat", "ft",
}


def title_case_artist(name: str) -> str:
    """
    Music-aware title case:
    - ALL-CAPS tokens of 2-5 chars are kept (EBM, AC, NIN, KMFDM …)
    - Tokens containing a digit or slash are kept verbatim (AC/DC, M83)
    - Common small words are lowercased except at position 0
    - Everything else: first letter capitalised
    """
    words = name.split()
    out = []
    for i, w in enumerate(words):
        if w.isupper() and 2 <= len(w) <= 5:        # acronym
            out.append(w)
        elif re.search(r"[\d/]", w):                 # has digit or slash
            out.append(w)
        elif w.lower() in _SMALL and i > 0:          # small connector
            out.append(w.lower())
        else:
            out.append(w[0].upper() + w[1:] if w else w)
    return " ".join(out)


def normalize_artist(raw: str) -> str:
    """
    Full artist normalisation pipeline:
      feat. suffix stripped → decade prefix stripped → title-cased
    Entries that are blank or placeholder strings are returned as "".
    """
    if not raw:
        return ""
    low = raw.strip().lower()
    if low in ("unknown", "<unknown>", "unknown artist", "various artists"):
        return ""
    a = FEAT_RE.sub("", raw.strip()).strip()
    a = DECADE_RE.sub("", a).strip()
    return title_case_artist(a)


def build_the_renames(artist_counts: dict[str, int]) -> dict[str, str]:
    """
    For every ("The X", "X") pair, rename the minority form to match
    whichever has more tracks in the collection.
    Returns {minority_name: canonical_name}.
    """
    renames: dict[str, str] = {}
    for name in list(artist_counts):
        if not name.startswith("The "):
            continue
        bare = name[4:]
        bare_count = artist_counts.get(bare, 0)
        if bare_count == 0:
            continue
        the_count = artist_counts[name]
        if the_count >= bare_count:
            renames[bare] = name       # "X" → "The X"
        else:
            renames[name] = bare       # "The X" → "X"
    return renames

# ─────────────────────────────────────────────────────────────────────────────
# Genre normalization
# ─────────────────────────────────────────────────────────────────────────────

def canonicalize_genre(raw: str) -> str | None:
    """
    Map a raw genre string to a canonical genre from GENRE_TAXONOMY.
    Handles multi-value strings ("Electronic / Industrial") by splitting
    and returning the highest-priority canonical match.
    Returns None if nothing maps.
    """
    if not raw:
        return None
    parts = re.split(r"[/;,]", raw)
    hits: list[str] = []
    for part in parts:
        key = part.strip().lower()
        mapped = GENRE_MAP.get(key)
        if mapped:
            hits.append(mapped)
    if not hits:
        return None
    for canonical in GENRE_TAXONOMY:
        if canonical in hits:
            return canonical
    return hits[0]

# ─────────────────────────────────────────────────────────────────────────────
# Artist recovery from filename
# ─────────────────────────────────────────────────────────────────────────────

def recover_artist_from_title(
    title: str,
    known_artists: set[str],
) -> tuple[str, str] | None:
    """
    If the title looks like "Artist - Title" or "Title - Artist",
    and exactly one side matches a known artist (case-insensitive),
    return (canonical_artist, clean_title).
    Returns None if no confident match.
    """
    m = DASH_RE.match(title)
    if not m:
        return None

    left  = re.sub(r"^\d{1,3}[\s.\-]+", "", m.group(1)).strip()  # strip track #
    right = m.group(2).strip()

    lower_to_canon = {a.lower(): a for a in known_artists}

    for candidate, other in [(left, right), (right, left)]:
        canon = lower_to_canon.get(candidate.lower())
        if canon:
            return canon, other
    return None

# ─────────────────────────────────────────────────────────────────────────────
# WAV → MP3 conversion
# ─────────────────────────────────────────────────────────────────────────────

def convert_wav_to_mp3(wav_path: str) -> str | None:
    """
    Convert wav_path → same directory, same basename, .mp3 extension.
    Uses LAME V0 VBR (≈ 220–260 kbps).  Returns mp3 path on success.
    """
    mp3_path = os.path.splitext(wav_path)[0] + ".mp3"
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", wav_path,
            "-codec:a", "libmp3lame",
            "-q:a", "0",           # LAME V0 VBR
            "-map_metadata", "0",  # copy all tags from source
            "-id3v2_version", "3", # ID3v2.3 — Traktor-compatible
            mp3_path,
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"    [ffmpeg] {os.path.basename(wav_path)}: "
              f"{result.stderr.decode(errors='replace')[-200:]}")
        return None
    return mp3_path

# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Produce a clean curated copy of a Traktor library.")
    parser.add_argument(
        "--apply", action="store_true",
        help="Execute (copy files, write NML). Default is dry-run.")
    args = parser.parse_args()

    ET.register_namespace("", "")

    SCRIPT_DIR  = Path(__file__).parent
    CURATED     = SCRIPT_DIR / "curated_music"
    NML_OUT_DIR = SCRIPT_DIR / "curated_traktor"

    # ── Find source NML ──────────────────────────────────────────────────────
    try:
        src_nml = find_source_nml()
    except FileNotFoundError as exc:
        sys.exit(str(exc))

    print(f"Source NML   : {src_nml}")
    print(f"Output music : {CURATED}")
    print(f"Output NML   : {NML_OUT_DIR / 'collection.nml'}")
    print()

    tree = ET.parse(src_nml)
    root = tree.getroot()
    collection = root.find("COLLECTION")
    if collection is None:
        sys.exit("No <COLLECTION> element found in NML.")

    all_entries = collection.findall("ENTRY")
    print(f"Source entries : {len(all_entries):,}")

    # ── First pass: filter + dedup ───────────────────────────────────────────
    # We materialise every surviving entry into hash_to_winner, keeping
    # the copy with the richest metadata when duplicates are found.

    hash_to_winner: dict[str, ET.Element] = {}   # md5 → best entry
    hash_to_path:   dict[str, str]        = {}   # md5 → abs path of best entry
    skip: collections.Counter = collections.Counter()

    print("Scanning … (this can take a few minutes on large libraries)")

    for entry in all_entries:
        # Location
        loc = entry.find("LOCATION")
        if loc is None:
            skip["no LOCATION"] += 1
            continue
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", ""))

        # File must exist on disk
        if not os.path.exists(path):
            skip["file missing"] += 1
            continue

        # Duration
        pt = entry_playtime(entry)
        if 0 < pt < MIN_PLAYTIME:
            skip[f"< {MIN_PLAYTIME}s (clip/intro)"] += 1
            continue
        if pt > MAX_PLAYTIME:
            skip[f"> {MAX_PLAYTIME//60} min (long recording)"] += 1
            continue

        # Live keyword
        title = entry.get("TITLE", "")
        fname = loc.get("FILE", "")
        if LIVE_RE.search(f"{title} {fname}"):
            skip["live recording"] += 1
            continue

        # Content hash
        try:
            h = file_md5(path)
        except OSError as exc:
            skip[f"hash error: {exc}"] += 1
            continue

        if h in hash_to_winner:
            # Duplicate — keep the entry with more artist/title metadata
            existing = hash_to_winner[h]
            score_new = len(entry.get("ARTIST", "")) + len(entry.get("TITLE", ""))
            score_old = len(existing.get("ARTIST", "")) + len(existing.get("TITLE", ""))
            if score_new > score_old:
                hash_to_winner[h] = entry
                hash_to_path[h]   = path
            skip["duplicate content"] += 1
            continue

        hash_to_winner[h] = entry
        hash_to_path[h]   = path

    keep = list(hash_to_winner.values())

    print(f"Entries kept   : {len(keep):,}")
    print(f"Entries dropped:")
    for reason, count in sorted(skip.items(), key=lambda x: -x[1]):
        print(f"  {count:6,}  {reason}")

    if not args.apply:
        print("\nDry-run complete.  Re-run with --apply to execute.")
        return

    # ── Normalize artists ────────────────────────────────────────────────────
    print("\nNormalizing artists …")

    for entry in keep:
        raw = entry.get("ARTIST", "")
        entry.set("ARTIST", normalize_artist(raw))

    artist_counts: dict[str, int] = collections.Counter(
        e.get("ARTIST", "") for e in keep if e.get("ARTIST", ""))

    the_renames = build_the_renames(dict(artist_counts))
    for entry in keep:
        a = entry.get("ARTIST", "")
        if a in the_renames:
            entry.set("ARTIST", the_renames[a])
    print(f"  'The X' / 'X' merges : {len(the_renames)}")

    # ── Recover artist from filename ─────────────────────────────────────────
    known_artists: set[str] = {e.get("ARTIST", "") for e in keep if e.get("ARTIST", "")}
    recovered = 0
    for entry in keep:
        if entry.get("ARTIST", "").strip():
            continue
        result = recover_artist_from_title(entry.get("TITLE", ""), known_artists)
        if result:
            artist, clean_title = result
            entry.set("ARTIST", artist)
            entry.set("TITLE",  clean_title)
            recovered += 1
    print(f"  Artist recovered from title : {recovered}")

    # ── Normalize genres ─────────────────────────────────────────────────────
    print("Normalizing genres …")

    # Build artist → genre counter from entries that already have a genre
    artist_genre: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    for entry in keep:
        a    = entry.get("ARTIST", "")
        info = entry.find("INFO")
        if not a or info is None:
            continue
        raw_g = info.get("GENRE", "").strip()
        canon = canonicalize_genre(raw_g)
        if canon:
            artist_genre[a][canon] += 1

    genre_mapped = genre_inferred = 0
    for entry in keep:
        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")
        raw_g  = info.get("GENRE", "").strip()
        artist = entry.get("ARTIST", "")

        canon = canonicalize_genre(raw_g)
        if canon:
            if canon != raw_g:
                info.set("GENRE", canon)
                genre_mapped += 1
        elif artist and artist_genre.get(artist):
            inferred = artist_genre[artist].most_common(1)[0][0]
            info.set("GENRE", inferred)
            genre_inferred += 1

    print(f"  Genres mapped    : {genre_mapped}")
    print(f"  Genres inferred  : {genre_inferred}")

    # ── Copy files & convert WAVs ────────────────────────────────────────────
    print(f"\nCopying {len(keep):,} files …")
    CURATED.mkdir(parents=True, exist_ok=True)
    NML_OUT_DIR.mkdir(parents=True, exist_ok=True)

    copied = converted = already_present = wav_errors = 0

    for entry in keep:
        src_path = entry_abs_path(entry)
        if not os.path.exists(src_path):
            continue

        artist = safe_name(entry.get("ARTIST", "") or "Unknown Artist")
        album  = safe_name(entry_album(entry) or "Unknown Album")
        src_p  = Path(src_path)

        dest_dir = CURATED / artist / album
        dest_dir.mkdir(parents=True, exist_ok=True)

        is_wav = src_p.suffix.lower() == ".wav"

        if is_wav:
            # Convert to MP3 first (into a temp location next to source)
            mp3_path = convert_wav_to_mp3(src_path)
            if mp3_path is None:
                wav_errors += 1
                continue
            dest_file = dest_dir / Path(mp3_path).name
            shutil.move(mp3_path, dest_file)
            converted += 1
            final_path = str(dest_file)
        else:
            dest_file = dest_dir / src_p.name
            if dest_file.exists():
                already_present += 1
            else:
                shutil.copy2(src_path, dest_file)
                copied += 1
            final_path = str(dest_file)

        # Update this entry's LOCATION to point at the new curated path
        loc = entry.find("LOCATION")
        new_loc = abs_to_traktor_location(final_path)
        loc.set("VOLUME", new_loc["VOLUME"])
        loc.set("DIR",    new_loc["DIR"])
        loc.set("FILE",   new_loc["FILE"])
        info = entry.find("INFO")
        if info is not None and is_wav:
            info.set("FILETYPE", "MP3")

    print(f"  Copied          : {copied:,}")
    print(f"  WAV → MP3       : {converted:,}")
    print(f"  Already present : {already_present:,}")
    if wav_errors:
        print(f"  WAV errors      : {wav_errors}")

    # ── Remove non-surviving entries from the NML tree ───────────────────────
    keep_ids = {id(e) for e in keep}
    for entry in list(collection.findall("ENTRY")):
        if id(entry) not in keep_ids:
            collection.remove(entry)
    collection.set("ENTRIES", str(len(keep)))

    # ── Write output NML ─────────────────────────────────────────────────────
    out_nml = NML_OUT_DIR / "collection.nml"
    tree.write(str(out_nml), encoding="UTF-8", xml_declaration=True)
    fix_xml_declaration(out_nml)

    print(f"\nWrote {out_nml}")
    print(f"\nDone.  {len(keep):,} tracks in curated library.")
    print(f"Point Traktor's root folder at:  {NML_OUT_DIR.parent}")


if __name__ == "__main__":
    main()
