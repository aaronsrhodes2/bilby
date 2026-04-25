#!/usr/bin/env python3
"""
tools/add_track.py — Single-track ingestion pipeline for Mac Bilby.

Drops one audio file all the way into the library:
  1.  SHA256 dedup check vs corrected_music/
  2.  AcoustID fingerprint → MusicBrainz metadata
  3.  Fallback: existing ID3/MP4/FLAC tags
  4.  Copy + tag → corrected_music/{Artist}/{Album (Year)}/
  5.  Add ENTRY to corrected_traktor/collection.nml
  6.  Fetch lyrics (lyrics.ovh → lrclib → genius)
  7.  Summarize with Claude Haiku
  8.  Write KEY_LYRICS + COMMENT2 to NML
  9.  Fetch + embed album art
  10. Autocue (vocal onset + drop) via librosa  [skipped if --no-cues]
  11. Apply cues to NML
  12. rclone upload to gdrive:Music/             [skipped if --no-upload]
  13. git commit NML + push                      [skipped if --no-upload]

Usage:
  python3 tools/add_track.py /path/to/file.mp3
  python3 tools/add_track.py /path/to/file.mp3 --no-cues
  python3 tools/add_track.py /path/to/file.mp3 --no-upload
  python3 tools/add_track.py /path/to/file.mp3 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

BASE      = Path(__file__).resolve().parent.parent
STATE_DIR = BASE / "state"
NML_PATH  = BASE / "corrected_traktor" / "collection.nml"
MUSIC_ROOT = Path(os.environ.get("MUSIC_ROOT", str(BASE / "corrected_music")))
FPCALC     = os.environ.get("FPCALC", "/opt/homebrew/bin/fpcalc")
ACOUSTID_KEY = os.environ.get("ACOUSTID_API_KEY", "")

# Traktor reports volume as "Macintosh HD" on Mac
TRAKTOR_VOLUME = "Macintosh HD"

sys.path.insert(0, str(BASE))


# ── Progress helper ───────────────────────────────────────────────────────────

def _prog(msg: str, q=None):
    """Print progress. If a queue is supplied (Flask mode), put a dict onto it."""
    print(f"  {msg}", flush=True)
    if q is not None:
        q.put({"msg": msg, "done": False})


def _done(ok: bool, msg: str, q=None):
    print(f"\n{'✓' if ok else '✗'} {msg}", flush=True)
    if q is not None:
        q.put({"msg": msg, "done": True, "ok": ok})


# ── SHA256 dedup ──────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing_hashes() -> set[str]:
    """Collect SHA256 of every file already in corrected_music/."""
    hashes = set()
    for p in MUSIC_ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aiff"}:
            try:
                hashes.add(sha256_file(p))
            except OSError:
                pass
    return hashes


# ── Fingerprint + metadata ────────────────────────────────────────────────────

def _run_fpcalc(path: Path) -> tuple[str, int] | None:
    """Run fpcalc synchronously. Returns (fingerprint, duration_s) or None."""
    fpcalc = shutil.which("fpcalc") or FPCALC
    if not Path(fpcalc).exists():
        return None
    try:
        result = subprocess.run(
            [fpcalc, "-json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        fp  = data.get("fingerprint", "")
        dur = int(data.get("duration", 0))
        return (fp, dur) if fp else None
    except Exception:
        return None


async def _lookup_acoustid(fingerprint: str, duration: int) -> str | None:
    """Return best MusicBrainz recording ID from AcoustID, or None."""
    if not ACOUSTID_KEY:
        return None
    from lib.acoustid_client import AcoustIDClient
    async with AcoustIDClient(ACOUSTID_KEY) as client:
        result = await client.lookup(fingerprint, duration)
    if not result:
        return None
    # result is list of {id, score, recordings: [{id, ...}]}
    best = max(result, key=lambda r: r.get("score", 0), default=None)
    if best:
        recs = best.get("recordings", [])
        if recs:
            return recs[0]["id"]
    return None


async def _lookup_mb(recording_id: str) -> dict | None:
    """Fetch MusicBrainz metadata for a recording ID."""
    from lib.mb_client import MusicBrainzClient
    async with MusicBrainzClient() as mb:
        return await mb.get_recording(recording_id)


def _fingerprint_meta(src: Path) -> dict:
    """
    Run fpcalc → AcoustID → MusicBrainz.
    Returns best available metadata dict (may be empty if nothing found).
    """
    fp_result = _run_fpcalc(src)
    if not fp_result:
        return {}
    fingerprint, duration = fp_result
    recording_id = asyncio.run(_lookup_acoustid(fingerprint, duration))
    if not recording_id:
        return {}
    meta = asyncio.run(_lookup_mb(recording_id))
    return meta or {}


def _tag_meta(src: Path) -> dict:
    """Read existing ID3/MP4/FLAC tags from file."""
    try:
        import mutagen
        from mutagen.mp3 import MP3
        from mutagen.mp4 import MP4
        from mutagen.flac import FLAC
        from mutagen._file import FileType

        ext = src.suffix.lower()
        tags: dict = {}

        if ext == ".mp3":
            audio = MP3(src)
            id3 = audio.tags
            if id3:
                tags["artist"] = str(id3.get("TPE1", [""])[0])
                tags["title"]  = str(id3.get("TIT2", [""])[0])
                tags["album"]  = str(id3.get("TALB", [""])[0])
                tags["year"]   = str(id3.get("TDRC", [""])[0])[:4] if id3.get("TDRC") else ""
                tags["genre"]  = str(id3.get("TCON", [""])[0])
                trck = str(id3.get("TRCK", [""])[0])
                tags["track_number"] = trck.split("/")[0] if trck else ""
            tags["duration"] = audio.info.length
            tags["bitrate"]  = int(audio.info.bitrate)
        elif ext in (".m4a", ".mp4", ".aac"):
            audio = MP4(src)
            t = audio.tags or {}
            tags["artist"] = str(t.get("©ART", [""])[0])
            tags["title"]  = str(t.get("©nam", [""])[0])
            tags["album"]  = str(t.get("©alb", [""])[0])
            yr = t.get("©day", [""])
            tags["year"] = str(yr[0])[:4] if yr else ""
            trkn = t.get("trkn", [(0,)])
            tags["track_number"] = str(trkn[0][0]) if trkn and trkn[0] else ""
            tags["duration"] = audio.info.length
            tags["bitrate"]  = int(audio.info.bitrate)
        elif ext == ".flac":
            audio = FLAC(src)
            tags["artist"] = (audio.get("artist") or [""])[0]
            tags["title"]  = (audio.get("title")  or [""])[0]
            tags["album"]  = (audio.get("album")  or [""])[0]
            tags["year"]   = (audio.get("date")   or [""])[:4] if audio.get("date") else ""
            tags["track_number"] = (audio.get("tracknumber") or [""])[0]
            tags["duration"] = audio.info.length
            tags["bitrate"]  = int(audio.info.bits_per_raw_sample * audio.info.sample_rate * 2 // 1000) if hasattr(audio.info, "bits_per_raw_sample") else 0
        else:
            audio = mutagen.File(src, easy=True)
            if audio:
                tags["artist"] = (audio.get("artist") or [""])[0]
                tags["title"]  = (audio.get("title")  or [""])[0]
                tags["album"]  = (audio.get("album")  or [""])[0]
                tags["duration"] = audio.info.length if hasattr(audio.info, "length") else 0

        # Fallback: parse from filename "Artist - Title.ext"
        stem = src.stem
        m = re.match(r'^(.+?)\s*[-–—]\s*(.+)$', stem)
        if m:
            if not tags.get("artist"):
                tags["artist"] = m.group(1).strip()
            if not tags.get("title"):
                tags["title"] = m.group(2).strip()

        return {k: v for k, v in tags.items() if v}
    except Exception as e:
        print(f"    [tag read warn] {e}")
        return {}


def _merge_meta(fp_meta: dict, tag_meta: dict, src_name: str) -> dict:
    """Prefer fp_meta (AcoustID), fill gaps with tag_meta, fall back to filename."""
    meta = dict(fp_meta)
    for k, v in tag_meta.items():
        if not meta.get(k):
            meta[k] = v

    # Last resort: parse filename
    stem = Path(src_name).stem
    m = re.match(r'^(.+?)\s*[-–—]\s*(.+)$', stem)
    if m:
        if not meta.get("artist"):
            meta["artist"] = m.group(1).strip()
        if not meta.get("title"):
            meta["title"] = m.group(2).strip()
    if not meta.get("title"):
        meta["title"] = stem
    if not meta.get("artist"):
        meta["artist"] = "Unknown Artist"

    return meta


# ── Copy + tag ────────────────────────────────────────────────────────────────

def _copy_and_tag(src: Path, meta: dict, dry_run: bool) -> Path:
    """Copy file to corrected_music/ tree with cleaned tags. Returns dest path."""
    sys.path.insert(0, str(BASE))
    from stage4_copy import make_dest_path, write_tags

    ext = src.suffix.lower()
    dest = make_dest_path(
        dest_root=MUSIC_ROOT,
        artist=meta.get("artist", "Unknown Artist"),
        album=meta.get("album", ""),
        year=meta.get("year"),
        track_number=meta.get("track_number"),
        title=meta.get("title", src.stem),
        ext=ext,
        used_paths=set(),
    )

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        write_tags(dest, ext, meta)

    return dest


# ── NML entry ─────────────────────────────────────────────────────────────────

def _now_traktor_date() -> tuple[str, str]:
    """Return (MODIFIED_DATE, MODIFIED_TIME) in Traktor format."""
    now = datetime.datetime.now()
    date_str = f"{now.year}/{now.month}/{now.day}"
    time_secs = now.hour * 3600 + now.minute * 60 + now.second
    return date_str, str(time_secs)


def _add_nml_entry(dest: Path, meta: dict, duration_s: float,
                   bitrate: int, dry_run: bool) -> None:
    """Insert a new ENTRY into collection.nml for the given track."""
    from lib.nml_parser import abs_to_traktor_location

    if not NML_PATH.exists():
        print(f"    [skip NML] {NML_PATH} not found")
        return

    tree = ET.parse(NML_PATH)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        print("    [skip NML] no COLLECTION element")
        return

    loc = abs_to_traktor_location(str(dest))
    date_str, time_str = _now_traktor_date()
    today_slash = datetime.date.today().strftime("%Y/%-m/%-d") if sys.platform != "win32" else \
                  datetime.date.today().strftime("%Y/%#m/%#d")
    filesize_kb = int(dest.stat().st_size / 1024) if dest.exists() else 0

    entry = ET.Element("ENTRY",
        MODIFIED_DATE=date_str,
        MODIFIED_TIME=time_str,
        AUDIO_ID="",
        TITLE=meta.get("title", dest.stem),
        ARTIST=meta.get("artist", "Unknown Artist"),
    )
    ET.SubElement(entry, "LOCATION",
        DIR=loc["DIR"], FILE=loc["FILE"],
        VOLUME=loc["VOLUME"], VOLUMEID=loc["VOLUMEID"],
    )
    if meta.get("album"):
        ET.SubElement(entry, "ALBUM",
            TRACK=str(meta.get("track_number") or ""),
            TITLE=meta.get("album", ""),
        )
    ET.SubElement(entry, "MODIFICATION_INFO", AUTHOR_TYPE="user")

    info_attrs: dict[str, str] = {
        "BITRATE":       str(bitrate) if bitrate else "0",
        "GENRE":         meta.get("genre", ""),
        "PLAYTIME":      str(int(duration_s)),
        "PLAYTIME_FLOAT": f"{duration_s:.6f}",
        "IMPORT_DATE":   today_slash,
        "FLAGS":         "12",
        "FILESIZE":      str(filesize_kb),
    }
    if meta.get("year"):
        info_attrs["RELEASE_DATE"] = f"{meta['year']}/1/1"
    ET.SubElement(entry, "INFO", **info_attrs)

    if not dry_run:
        coll.append(entry)
        tree.write(NML_PATH, encoding="utf-8", xml_declaration=True)


# ── Update NML INFO attributes (lyrics / comment) ────────────────────────────

def _update_nml_info(dest: Path, comment: str, comment2: str,
                     key_lyrics: str, dry_run: bool) -> None:
    """Set COMMENT, COMMENT2, KEY_LYRICS on the NML entry for this file."""
    from lib.nml_parser import traktor_to_abs

    if not NML_PATH.exists() or dry_run:
        return

    tree = ET.parse(NML_PATH)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        return

    dest_str = str(dest).replace("\\", "/")
    for entry in coll.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        try:
            abs_path = traktor_to_abs(
                loc.get("VOLUME", ""),
                loc.get("DIR", ""),
                loc.get("FILE", ""),
            )
        except Exception:
            continue
        if abs_path.replace("\\", "/") != dest_str:
            continue
        info = entry.find("INFO")
        if info is None:
            info = ET.SubElement(entry, "INFO")
        if comment:
            info.set("COMMENT", comment)
        if comment2:
            info.set("COMMENT2", comment2)
        if key_lyrics:
            info.set("KEY_LYRICS", key_lyrics)
        tree.write(NML_PATH, encoding="utf-8", xml_declaration=True)
        return


# ── Apply cues to NML ─────────────────────────────────────────────────────────

def _apply_cues(dest: Path, cue_data: dict, dry_run: bool) -> None:
    """Write CUE_V2 elements into the NML entry for this file."""
    if not cue_data or not NML_PATH.exists() or dry_run:
        return

    from lib.nml_parser import traktor_to_abs

    tree = ET.parse(NML_PATH)
    root = tree.getroot()
    coll = root.find("COLLECTION")
    if coll is None:
        return

    dest_str = str(dest).replace("\\", "/")
    for entry in coll.findall("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is None:
            continue
        try:
            abs_path = traktor_to_abs(
                loc.get("VOLUME", ""),
                loc.get("DIR", ""),
                loc.get("FILE", ""),
            )
        except Exception:
            continue
        if abs_path.replace("\\", "/") != dest_str:
            continue

        if "vocal_ms" in cue_data:
            ET.SubElement(entry, "CUE_V2",
                NAME="Vocal In", DISPL_ORDER="2", TYPE="0",
                START=f"{cue_data['vocal_ms']:.6f}", LEN="0.000000",
                REPEATS="-1", HOTCUE="2",
            )
        if "drop_ms" in cue_data:
            loop_len = cue_data.get("drop_len_ms", 0.0)
            ET.SubElement(entry, "CUE_V2",
                NAME="Drop", DISPL_ORDER="3", TYPE="4",
                START=f"{cue_data['drop_ms']:.6f}", LEN=f"{loop_len:.6f}",
                REPEATS="-1", HOTCUE="3",
            )
        tree.write(NML_PATH, encoding="utf-8", xml_declaration=True)
        return


# ── Lyrics ────────────────────────────────────────────────────────────────────

def _fetch_lyrics(artist: str, title: str) -> tuple[str | None, str | None]:
    """Try lyrics.ovh → lrclib → genius. Returns (plain_text, lrc_string)."""
    from stage9_lyrics import fetch_lyrics, fetch_lyrics_lrclib, fetch_lyrics_genius, LYRICS_LRC

    # lyrics.ovh (no LRC available from this source)
    text = fetch_lyrics(artist, title)
    if text:
        return text, None

    # lrclib — also saves syncedLyrics LRC if available
    text, is_instrumental, lrc = fetch_lyrics_lrclib(artist, title)
    if lrc:
        # Persist LRC to the cache file so Bilby can serve it
        dk = f"{artist.lower().strip()}\t{title.lower().strip()}"
        try:
            cache: dict = json.loads(LYRICS_LRC.read_text(encoding="utf-8")) if LYRICS_LRC.exists() else {}
            cache[dk] = lrc
            LYRICS_LRC.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if is_instrumental:
        return None, None
    if text:
        return text, lrc

    # genius (slow, last resort — no LRC)
    return fetch_lyrics_genius(artist, title), None


def _summarize(artist: str, title: str, lyrics: str) -> dict:
    """Summarize via Claude Haiku (stage9_stt_mac.summarise)."""
    try:
        sys.path.insert(0, str(BASE))
        from stage9_stt_mac import summarise
        result = summarise(artist, title, lyrics)
        return result or {}
    except Exception as e:
        print(f"    [summarize warn] {e}")
        return {}


def _build_comment2(summary_result: dict) -> str:
    """Build COMMENT2 string: 'theme | ⚑flag1 ⚑flag2 | ...'"""
    parts = []
    theme = summary_result.get("theme", "")
    if theme:
        parts.append(theme)
    flags = summary_result.get("flags", [])
    if flags:
        parts.append(" ".join(f"⚑{f}" for f in flags))
    return " | ".join(parts) if parts else ""


# ── Album art ─────────────────────────────────────────────────────────────────

def _fetch_and_embed_art(dest: Path, artist: str, title: str,
                          album: str, dry_run: bool) -> None:
    """Fetch album art (Spotify → iTunes → MusicBrainz) and embed in file."""
    try:
        sys.path.insert(0, str(BASE / "tools"))
        from fetch_album_art import SpotifyClient, _fetch_itunes_art

        art_url: str | None = None

        # Try Spotify
        try:
            sp = SpotifyClient()
            art_url = sp.search_art_url(artist, title)
        except Exception:
            pass

        # Try iTunes
        if not art_url:
            try:
                art_url = _fetch_itunes_art(artist, album or title)
            except Exception:
                pass

        if not art_url or dry_run:
            return

        import urllib.request, io
        with urllib.request.urlopen(art_url, timeout=10) as r:
            art_bytes = r.read()

        ext = dest.suffix.lower()
        if ext == ".mp3":
            from mutagen.id3 import ID3, APIC, ID3NoHeaderError
            try:
                tags = ID3(dest)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3,
                          desc="Cover", data=art_bytes))
            tags.save(dest)
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            tags = MP4(dest)
            tags["covr"] = [MP4Cover(art_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            tags.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(dest)
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = art_bytes
            audio.add_picture(pic)
            audio.save()
    except Exception as e:
        print(f"    [art warn] {e}")


# ── Autocue ───────────────────────────────────────────────────────────────────

def _compute_cues(dest: Path) -> dict:
    """Run librosa autocue on dest. Returns {} if librosa not installed."""
    try:
        sys.path.insert(0, str(BASE / "tools"))
        from compute_cues_pc import compute_cues
        return compute_cues(dest)
    except SystemExit:
        return {}  # librosa not installed; skip cues
    except Exception as e:
        print(f"    [cue warn] {e}")
        return {}


# ── Drive upload + git ────────────────────────────────────────────────────────

def _rclone_upload(dest: Path, meta: dict, dry_run: bool) -> bool:
    """Upload the new track to gdrive:Music/{Artist}/{Album}/"""
    rclone = shutil.which("rclone")
    if not rclone:
        print("    [skip upload] rclone not found in PATH")
        return False

    from stage4_copy import sanitize
    artist_s = sanitize(meta.get("artist", "Unknown Artist"))
    album    = meta.get("album", "")
    year     = meta.get("year")
    if album:
        from stage4_copy import sanitize as _s
        album_s = _s(album)
        folder_name = f"{album_s} ({year})" if year else album_s
    else:
        folder_name = "Unknown Album"

    remote_dir = f"gdrive:Music/{artist_s}/{folder_name}/"
    cmd = [rclone, "copyto", str(dest), f"{remote_dir}{dest.name}", "--progress"]

    if dry_run:
        print(f"    [dry-run] rclone copyto {dest.name} → {remote_dir}")
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    [upload warn] rclone: {result.stderr.strip()[:200]}")
        return False
    return True


def _git_commit_push(dry_run: bool) -> None:
    """Commit the updated NML and push to origin."""
    if dry_run:
        print("    [dry-run] git commit + push skipped")
        return
    try:
        subprocess.run(
            ["git", "-C", str(BASE), "add", str(NML_PATH)],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(BASE), "commit", "-m",
             "add_track: new track added via Mac Bilby drop zone"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(BASE), "push"],
            check=True, capture_output=True,
        )
        print("    NML committed and pushed — PC Bilby will see the new track on next pull.")
    except subprocess.CalledProcessError as e:
        print(f"    [git warn] {e.stderr.decode(errors='replace').strip()[:200]}")


# ── Main pipeline ─────────────────────────────────────────────────────────────

def add_track(src: Path, no_cues: bool = False, no_upload: bool = False,
              dry_run: bool = False, progress_queue=None) -> dict:
    """
    Full single-track ingestion pipeline.
    progress_queue: optional queue.Queue for Flask SSE progress streaming.
    Returns result dict with keys: ok, dest, artist, title, error.
    """
    p = lambda msg: _prog(msg, progress_queue)

    try:
        if not src.exists():
            raise FileNotFoundError(f"{src} not found")

        # ── 1. SHA256 dedup ────────────────────────────────────────────────────
        p(f"Computing SHA256 for {src.name}…")
        new_hash = sha256_file(src)
        existing = _existing_hashes()
        if new_hash in existing:
            raise ValueError(f"Track already in library (SHA256 match). Skipping.")

        # ── 2. Fingerprint + metadata ──────────────────────────────────────────
        p("Running AcoustID fingerprint…")
        fp_meta = _fingerprint_meta(src)
        if fp_meta:
            p(f"  AcoustID match: {fp_meta.get('artist')} — {fp_meta.get('title')}")
        else:
            p("  No AcoustID match — falling back to tags")

        # ── 3. Tag fallback ────────────────────────────────────────────────────
        p("Reading existing tags…")
        tag_meta = _tag_meta(src)
        meta = _merge_meta(fp_meta, tag_meta, src.name)
        artist = meta.get("artist", "Unknown Artist")
        title  = meta.get("title", src.stem)
        p(f"  Resolved: {artist} — {title}")

        duration_s = float(meta.get("duration") or 0)
        bitrate    = int(meta.get("bitrate") or 0)

        # ── 4. Copy + tag ──────────────────────────────────────────────────────
        p(f"Copying to corrected_music/…")
        dest = _copy_and_tag(src, meta, dry_run)
        p(f"  → {dest.relative_to(BASE)}")

        # ── 5. Add NML entry ───────────────────────────────────────────────────
        p("Adding NML entry…")
        _add_nml_entry(dest, meta, duration_s, bitrate, dry_run)

        # ── 6. Fetch lyrics ────────────────────────────────────────────────────
        p(f"Fetching lyrics for {artist} — {title}…")
        lyrics, lrc = _fetch_lyrics(artist, title)
        if lyrics:
            p(f"  Lyrics found ({len(lyrics):,} chars)")
            if lrc:
                p(f"  Synced LRC found ({len(lrc):,} chars)")
        else:
            p("  No lyrics found")

        # ── 7. Summarize ───────────────────────────────────────────────────────
        summary_result: dict = {}
        if lyrics:
            p("Summarizing with Claude Haiku…")
            summary_result = _summarize(artist, title, lyrics)
            if summary_result.get("summary"):
                p(f"  Summary: {summary_result['summary'][:80]}")

        comment  = summary_result.get("summary", "")
        comment2 = _build_comment2(summary_result)

        # ── 8. Write NML metadata ──────────────────────────────────────────────
        p("Writing metadata to NML…")
        _update_nml_info(dest, comment, comment2, lyrics or "", dry_run)

        # ── 9. Album art ───────────────────────────────────────────────────────
        p("Fetching album art…")
        _fetch_and_embed_art(dest, artist, title, meta.get("album", ""), dry_run)

        # ── 10–11. Autocue ─────────────────────────────────────────────────────
        if not no_cues:
            p("Computing cues (librosa)…")
            cue_data = _compute_cues(dest)
            if cue_data:
                p(f"  Vocal in: {cue_data.get('vocal_ms', '—')} ms  "
                  f"Drop: {cue_data.get('drop_ms', '—')} ms")
                _apply_cues(dest, cue_data, dry_run)
            else:
                p("  librosa not available or no cues detected — skipping")
        else:
            p("Skipping autocue (--no-cues)")

        # ── 12. rclone upload ──────────────────────────────────────────────────
        if not no_upload:
            p("Uploading to Google Drive…")
            _rclone_upload(dest, meta, dry_run)

        # ── 13. git commit + push ──────────────────────────────────────────────
        if not no_upload:
            p("Committing NML to git…")
            _git_commit_push(dry_run)

        result = {"ok": True, "dest": str(dest), "artist": artist, "title": title}
        _done(True, f"Added: {artist} — {title}", progress_queue)
        return result

    except Exception as e:
        _done(False, f"Failed: {e}", progress_queue)
        return {"ok": False, "error": str(e)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file",        help="Audio file to add")
    ap.add_argument("--no-cues",   action="store_true", help="Skip librosa autocue")
    ap.add_argument("--no-upload", action="store_true", help="Skip Drive upload + git push")
    ap.add_argument("--dry-run",   action="store_true", help="Simulate, write nothing")
    args = ap.parse_args()

    src = Path(args.file).expanduser().resolve()
    result = add_track(src, no_cues=args.no_cues,
                       no_upload=args.no_upload, dry_run=args.dry_run)
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
