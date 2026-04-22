#!/usr/bin/env python3
"""
tools/drive_intake.py — Google Drive music intake pipeline.

Scans Google Drive for audio files not yet in the library, downloads them,
and runs them through the lyrics pipeline (fetch → summarize → export).

Useful for:
  • Artist-shared tracks (e.g. Faderhead sharing MP3s directly to your Drive)
  • Files you upload to Drive from a phone / secondary machine
  • Any audio that lands in Drive before it reaches the local collection

─────────────────────────────────────────────────────────────────────────────
FIRST-TIME SETUP
─────────────────────────────────────────────────────────────────────────────

1. Go to https://console.cloud.google.com/
2. Create a project (or reuse one)
3. Enable the "Google Drive API"
4. Create OAuth 2.0 credentials → Desktop app
5. Download the JSON → save as  state/drive_credentials.json
6. Run:  python tools/drive_intake.py --setup
   (Opens browser for one-time consent; saves token to state/drive_token.json)

─────────────────────────────────────────────────────────────────────────────
USAGE
─────────────────────────────────────────────────────────────────────────────

  python tools/drive_intake.py --scan              # list new audio in Drive
  python tools/drive_intake.py --intake            # download + add to library
  python tools/drive_intake.py --intake --process  # also fetch lyrics & summarize
  python tools/drive_intake.py --setup             # first-time OAuth

─────────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import os
import re
import sys
import time
import threading
from pathlib import Path

BASE      = Path(__file__).parent.parent
STATE_DIR = BASE / "state"
CREDS_FILE  = STATE_DIR / "drive_credentials.json"
TOKEN_FILE  = STATE_DIR / "drive_token.json"
KNOWN_FILE  = STATE_DIR / "drive_known.json"   # Drive file IDs already processed
TRACKLIST   = STATE_DIR / "tracklist.json"
LYRICS_RAW  = STATE_DIR / "lyrics_raw.json"

# Local music root — artist subdirs live here
MUSIC_ROOT = Path("D:/Aaron/Music/VERAS SONGS")

AUDIO_MIME_TYPES = [
    "audio/mpeg",           # MP3
    "audio/flac",           # FLAC
    "audio/x-flac",
    "audio/wav",            # WAV
    "audio/aiff",           # AIFF
    "audio/x-aiff",
    "audio/mp4",            # M4A
    "audio/ogg",            # OGG
]

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

MIN_AUDIO_BYTES = 1_000_000   # ignore files under 1 MB (test files, placeholders)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_drive_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDS_FILE.exists():
                print(f"ERROR: {CREDS_FILE} not found.")
                print("Download OAuth2 credentials from Google Cloud Console → save as state/drive_credentials.json")
                print("Then run: python tools/drive_intake.py --setup")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


# ── Drive helpers ─────────────────────────────────────────────────────────────

def list_audio_files(service) -> list[dict]:
    """Return all audio files in Drive (owned + shared with user)."""
    mime_query = " or ".join(f"mimeType = '{m}'" for m in AUDIO_MIME_TYPES)
    query      = f"({mime_query}) and trashed = false"

    files  = []
    token  = None
    while True:
        resp = service.files().list(
            q=query,
            pageSize=200,
            fields="nextPageToken, files(id, name, size, owners, sharedWithMeTime, modifiedTime, mimeType)",
            pageToken=token,
        ).execute()
        files.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break

    # Filter out tiny files (test fixtures, placeholders)
    return [f for f in files if int(f.get("size", 0)) >= MIN_AUDIO_BYTES]


def download_file(service, file_id: str, dest_path: Path) -> bool:
    """Download a Drive file to dest_path. Returns True on success."""
    from googleapiclient.http import MediaIoBaseDownload
    import io

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    request  = service.files().get_media(fileId=file_id)
    buf      = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=4 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest_path.write_bytes(buf.getvalue())
    return True


# ── Library helpers ───────────────────────────────────────────────────────────

_VERSION_RE  = re.compile(r'\s*[\(\[].{0,40}[\)\]]\s*$')
_ARTIST_SPLIT = re.compile(r'^(.+?)\s*[-–—]\s*(.+)$')


def parse_filename(name: str) -> tuple[str, str]:
    """
    Parse 'Artist - Title.mp3' → (artist, title).
    Falls back to ('Unknown', stem) if no dash present.
    """
    stem = Path(name).stem
    m = _ARTIST_SPLIT.match(stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "Unknown", stem


def base_title(title: str) -> str:
    return _VERSION_RE.sub("", title).strip().lower()


def dedup_key(artist: str, title: str) -> str:
    return f"{artist.lower().strip()}\t{base_title(title)}"


def load_known() -> set:
    if KNOWN_FILE.exists():
        return set(json.loads(KNOWN_FILE.read_text(encoding="utf-8")))
    return set()


def save_known(known: set) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    KNOWN_FILE.write_text(json.dumps(sorted(known), indent=2, ensure_ascii=False), encoding="utf-8")


def load_tracklist() -> list[dict]:
    if TRACKLIST.exists():
        return json.loads(TRACKLIST.read_text(encoding="utf-8"))
    return []


def save_tracklist(tracks: list[dict]) -> None:
    TRACKLIST.write_text(json.dumps(tracks, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_scan(service) -> None:
    """List all audio files in Drive, flagging which are new to the library."""
    known      = load_known()
    tracklist  = load_tracklist()
    known_dkeys = {t["dkey"] for t in tracklist}

    files = list_audio_files(service)
    print(f"Found {len(files)} audio files in Drive\n")

    new_count = 0
    for f in sorted(files, key=lambda x: x.get("modifiedTime", ""), reverse=True):
        fid      = f["id"]
        name     = f["name"]
        size_mb  = int(f.get("size", 0)) / 1024 / 1024
        artist, title = parse_filename(name)
        dkey     = dedup_key(artist, title)
        in_lib   = dkey in known_dkeys
        in_known = fid in known
        status   = "in library" if in_lib else ("downloaded" if in_known else "NEW")
        if status == "NEW":
            new_count += 1
        owner    = f.get("owners", [{}])[0].get("displayName", "?") if f.get("owners") else "shared"
        print(f"  [{status:12s}] {artist} — {title}  ({size_mb:.1f} MB, from: {owner})")

    print(f"\n{new_count} new track(s) not yet in library")


def cmd_intake(service, process: bool = False) -> None:
    """Download new Drive audio files and add them to the library."""
    known     = load_known()
    tracklist = load_tracklist()
    known_dkeys = {t["dkey"] for t in tracklist}

    files = list_audio_files(service)
    new_files = [f for f in files if f["id"] not in known]

    print(f"Drive audio files: {len(files)} total, {len(new_files)} not yet downloaded\n")
    if not new_files:
        print("Nothing to do.")
        return

    added = []
    for f in new_files:
        fid   = f["id"]
        name  = f["name"]
        artist, title = parse_filename(name)
        dkey  = dedup_key(artist, title)

        if dkey in known_dkeys:
            print(f"  [skip — in library] {artist} — {title}")
            known.add(fid)
            continue

        size_mb = int(f.get("size", 0)) / 1024 / 1024
        dest    = MUSIC_ROOT / artist / name
        print(f"  [downloading {size_mb:.1f} MB] {artist} — {title}")

        try:
            download_file(service, fid, dest)
            print(f"    → {dest}")
        except Exception as e:
            print(f"    [FAILED] {e}")
            continue

        track = {
            "artist": artist,
            "title":  title,
            "dkey":   dkey,
            "path":   str(dest),
            "source": "google_drive",
        }
        tracklist.append(track)
        known_dkeys.add(dkey)
        known.add(fid)
        added.append(track)

    save_known(known)
    if added:
        save_tracklist(tracklist)
        print(f"\nAdded {len(added)} track(s) to tracklist.json")

    if process and added:
        print("\nRunning lyrics pipeline on new tracks…")
        sys.path.insert(0, str(BASE))
        from stage9_lyrics import run_fetch, run_summarize, run_list, load_all_tracks, TRAKTOR_NML

        tracks = load_all_tracks(TRAKTOR_NML)
        run_fetch(tracks)
        run_summarize(tracks)
        out = str(STATE_DIR / "lyrics_summary.json")
        run_list(tracks, out_path=out)
        print(f"Updated {out}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Google Drive music intake pipeline")
    parser.add_argument("--setup",   action="store_true", help="Run OAuth setup flow")
    parser.add_argument("--scan",    action="store_true", help="List audio files in Drive")
    parser.add_argument("--intake",  action="store_true", help="Download new files to library")
    parser.add_argument("--process", action="store_true", help="Also run lyrics pipeline after intake")
    args = parser.parse_args()

    if not any([args.setup, args.scan, args.intake]):
        parser.print_help()
        sys.exit(0)

    print("Connecting to Google Drive…")
    service = get_drive_service()
    print("Connected.\n")

    if args.scan:
        cmd_scan(service)
    elif args.intake:
        cmd_intake(service, process=args.process)


if __name__ == "__main__":
    main()
