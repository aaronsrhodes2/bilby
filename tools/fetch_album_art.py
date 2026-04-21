#!/usr/bin/env python3
"""
fetch_album_art.py — Batch album art fetcher for DJ collection

Sources (in order):
  1. Spotify API (client-credentials, no user login required)
  2. MusicBrainz Cover Art Archive
  3. mutagen (embedded art in the local audio file)

Saves JPEGs to state/album_art/{md5_of_dkey}.jpg
Index:  state/album_art_index.json  →  {dkey: "/art/{filename}.jpg" | null}

Usage:
  python3 tools/fetch_album_art.py --report         # stats
  python3 tools/fetch_album_art.py --run             # full pass (resumable)
  python3 tools/fetch_album_art.py --run --limit 50  # test batch
  python3 tools/fetch_album_art.py --run --force     # retry nulls too
"""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

BASE       = Path(__file__).parent.parent
STATE      = BASE / "state"
ART_DIR    = STATE / "album_art"
INDEX_JSON = STATE / "album_art_index.json"
NML_PATH   = BASE / "corrected_traktor" / "collection.nml"
CREDS_FILE = BASE / "spotify_creds.txt"

sys.path.insert(0, str(BASE))
from lib.nml_parser import traktor_to_abs

SAVE_EVERY    = 100
SPOTIFY_DELAY = 0.12   # seconds between Spotify calls (~8 req/s, limit is 180/30s)
MB_DELAY      = 1.1    # MusicBrainz politely requires ≥1s between requests

# ── Key helpers ────────────────────────────────────────────────────────────────

_VER = re.compile(r'\s*[\(\[][^)\]]{0,40}[\)\]]\s*$')

def _clean_title(title: str) -> str:
    """Strip version markers for cleaner API searches."""
    return _VER.sub("", title or "").strip()

def dkey(artist: str, title: str) -> str:
    """Canonical dedup key matching stage9_dj_suggest._song_key()."""
    return f"{(artist or '').lower().strip()}\t{(title or '').lower().strip()}"

def art_filename(dk: str) -> str:
    return hashlib.md5(dk.encode()).hexdigest() + ".jpg"

def load_json(p: Path, default):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default

def save_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

# ── Build track list from NML ──────────────────────────────────────────────────

def load_tracks() -> list[dict]:
    """Return list of unique {artist, title, path, dkey} from NML."""
    tree = ET.parse(NML_PATH)
    coll = tree.getroot().find("COLLECTION")
    seen: set[str] = set()
    tracks = []
    for e in coll.findall("ENTRY"):
        artist = e.get("ARTIST", "").strip()
        title  = e.get("TITLE",  "").strip()
        if not artist and not title:
            continue
        dk = dkey(artist, title)
        if dk in seen:
            continue
        seen.add(dk)
        loc = e.find("LOCATION")
        path = ""
        if loc is not None:
            path = traktor_to_abs(
                loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
            )
        tracks.append({"artist": artist, "title": title, "path": path, "dkey": dk})
    return tracks

# ── Spotify ────────────────────────────────────────────────────────────────────

class SpotifyClient:
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"

    def __init__(self, client_id: str, client_secret: str):
        self.client_id     = client_id
        self.client_secret = client_secret
        self._token        = None
        self._token_expiry = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        creds = f"{self.client_id}:{self.client_secret}"
        b64   = __import__("base64").b64encode(creds.encode()).decode()
        data  = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req   = urllib.request.Request(
            self.TOKEN_URL, data=data,
            headers={"Authorization": f"Basic {b64}",
                     "Content-Type": "application/x-www-form-urlencoded"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        self._token        = body["access_token"]
        self._token_expiry = time.time() + body.get("expires_in", 3600)
        return self._token

    def search_art_url(self, artist: str, title: str) -> str | None:
        """Return a ~300px album art URL or None."""
        token = self._get_token()
        clean = _clean_title(title)
        for query in [
            f'artist:"{artist}" track:"{clean}"',
            f'"{artist}" "{clean}"',
        ]:
            params = urllib.parse.urlencode({
                "q": query, "type": "track", "limit": 1, "market": "US"
            })
            req = urllib.request.Request(
                f"{self.SEARCH_URL}?{params}",
                headers={"Authorization": f"Bearer {token}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = json.loads(r.read())
                items = body.get("tracks", {}).get("items", [])
                if items:
                    images = items[0].get("album", {}).get("images", [])
                    if images:
                        # images are sorted largest→smallest; index 1 is ~300px
                        idx = 1 if len(images) > 1 else 0
                        return images[idx].get("url")
            except Exception:
                pass
            time.sleep(SPOTIFY_DELAY)
        return None

def load_spotify_creds() -> SpotifyClient | None:
    if not CREDS_FILE.exists():
        print("  [warn] spotify_creds.txt not found — Spotify disabled")
        return None
    creds = {}
    for line in CREDS_FILE.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    cid = creds.get("client_id", "")
    sec = creds.get("client_secret", "")
    if not cid or not sec:
        print("  [warn] Spotify creds incomplete")
        return None
    return SpotifyClient(cid, sec)

# ── MusicBrainz Cover Art Archive ─────────────────────────────────────────────

def mb_search_art_url(artist: str, title: str) -> str | None:
    """Search MusicBrainz for a release MBID, then hit Cover Art Archive."""
    clean = _clean_title(title)
    params = urllib.parse.urlencode({
        "query":  f'artist:"{artist}" AND recording:"{clean}"',
        "fmt":    "json",
        "limit":  1,
    })
    try:
        req = urllib.request.Request(
            f"https://musicbrainz.org/ws/2/recording?{params}",
            headers={"User-Agent": "DJBlockPlanner/1.0 (aaron.s.rhodes@gmail.com)"}
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            body = json.loads(r.read())
        recordings = body.get("recordings", [])
        if not recordings:
            return None
        releases = recordings[0].get("releases", [])
        if not releases:
            return None
        mbid = releases[0].get("id", "")
        if not mbid:
            return None
        time.sleep(MB_DELAY)
        # Try Cover Art Archive thumbnail
        caa_url = f"https://coverartarchive.org/release/{mbid}/front-250"
        req2 = urllib.request.Request(
            caa_url,
            headers={"User-Agent": "DJBlockPlanner/1.0 (aaron.s.rhodes@gmail.com)"}
        )
        with urllib.request.urlopen(req2, timeout=12) as r:
            data = r.read()
        if len(data) > 1000:   # real image, not an error page
            return caa_url
    except Exception:
        pass
    return None

# ── mutagen embedded art ──────────────────────────────────────────────────────

def extract_embedded_art(path: str) -> bytes | None:
    """Extract embedded JPEG art bytes from audio file, or None."""
    if not path or not Path(path).exists():
        return None
    try:
        import mutagen
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.flac import FLAC
        ext = Path(path).suffix.lower()
        if ext in (".mp3", ".aiff", ".aif"):
            try:
                tags = ID3(path)
                for key in tags:
                    if key.startswith("APIC"):
                        return tags[key].data
            except Exception:
                pass
        elif ext == ".mp4" or ext == ".m4a":
            try:
                tags = MP4(path)
                covers = tags.get("covr", [])
                if covers:
                    return bytes(covers[0])
            except Exception:
                pass
        elif ext == ".flac":
            try:
                tags = FLAC(path)
                pics = tags.pictures
                if pics:
                    return pics[0].data
            except Exception:
                pass
    except ImportError:
        pass
    return None

# ── Image downloader ──────────────────────────────────────────────────────────

def download_image(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DJBlockPlanner/1.0 (aaron.s.rhodes@gmail.com)"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        if len(data) > 2000:
            return data
    except Exception:
        pass
    return None

def save_art(dk: str, data: bytes) -> str:
    """Save image bytes, return relative URL like /art/{filename}."""
    ART_DIR.mkdir(parents=True, exist_ok=True)
    fname = art_filename(dk)
    (ART_DIR / fname).write_bytes(data)
    return f"/art/{fname}"

# ── Report ────────────────────────────────────────────────────────────────────

def report():
    tracks = load_tracks()
    index  = load_json(INDEX_JSON, {})
    total      = len(tracks)
    found      = sum(1 for dk in (t["dkey"] for t in tracks) if index.get(dk) not in (None, ""))
    not_found  = sum(1 for dk in (t["dkey"] for t in tracks) if dk in index and index[dk] is None)
    unprocessed = total - found - not_found
    print(f"Unique tracks:     {total:,}")
    print(f"Art found:         {found:,}  ({found*100//total if total else 0}%)")
    print(f"Not found (null):  {not_found:,}")
    print(f"Not yet tried:     {unprocessed:,}")
    # Count files on disk
    if ART_DIR.exists():
        files = list(ART_DIR.glob("*.jpg"))
        total_mb = sum(f.stat().st_size for f in files) / 1024 / 1024
        print(f"Files on disk:     {len(files):,}  ({total_mb:.1f} MB)")

# ── Main run ──────────────────────────────────────────────────────────────────

def run(limit: int = 0, force: bool = False):
    ART_DIR.mkdir(parents=True, exist_ok=True)
    tracks = load_tracks()
    index  = load_json(INDEX_JSON, {})

    spotify = load_spotify_creds()
    if spotify:
        # Warm up token
        try:
            spotify._get_token()
            print("  Spotify: token OK")
        except Exception as ex:
            print(f"  Spotify: token failed — {ex}")
            spotify = None

    # Build todo list
    todo = []
    for t in tracks:
        dk = t["dkey"]
        if dk in index:
            if index[dk] is not None:
                continue   # already fetched
            if not force:
                continue   # null — skip unless --force
        todo.append(t)

    if limit:
        todo = todo[:limit]

    print(f"Tracks to process: {len(todo):,}  (of {len(tracks):,} unique)")
    if not todo:
        print("Nothing to do.")
        return

    done = fetched = mb_fetched = embedded = not_found = 0

    for i, track in enumerate(todo):
        artist = track["artist"]
        title  = track["title"]
        path   = track["path"]
        dk     = track["dkey"]
        art_url = None

        print(f"[{i+1}/{len(todo)}] {artist} — {title}")

        # ── Source 1: Spotify ──────────────────────────────────────────────────
        if spotify and not art_url:
            try:
                img_url = spotify.search_art_url(artist, title)
                if img_url:
                    data = download_image(img_url)
                    if data:
                        art_url = save_art(dk, data)
                        fetched += 1
                        print(f"  ✓ Spotify  ({len(data)//1024}KB)")
            except Exception as ex:
                print(f"  Spotify error: {ex}")
            time.sleep(SPOTIFY_DELAY)

        # ── Source 2: MusicBrainz CAA ─────────────────────────────────────────
        if not art_url:
            try:
                mb_url = mb_search_art_url(artist, title)
                if mb_url:
                    data = download_image(mb_url)
                    if data:
                        art_url = save_art(dk, data)
                        mb_fetched += 1
                        print(f"  ✓ MusicBrainz  ({len(data)//1024}KB)")
            except Exception as ex:
                print(f"  MusicBrainz error: {ex}")
            time.sleep(MB_DELAY)

        # ── Source 3: Embedded art via mutagen ────────────────────────────────
        if not art_url:
            try:
                data = extract_embedded_art(path)
                if data:
                    art_url = save_art(dk, data)
                    embedded += 1
                    print(f"  ✓ Embedded  ({len(data)//1024}KB)")
            except Exception as ex:
                print(f"  mutagen error: {ex}")

        # ── Record result ─────────────────────────────────────────────────────
        if art_url:
            index[dk] = art_url
        else:
            index[dk] = None
            not_found += 1
            print(f"  ✗ Not found")

        done += 1

        if done % SAVE_EVERY == 0:
            save_json(INDEX_JSON, index)
            print(f"  [saved — {done} done, {fetched}S/{mb_fetched}MB/{embedded}E/{not_found}✗]")

    # Final save
    save_json(INDEX_JSON, index)
    print(f"\nDone. {done} processed — "
          f"{fetched} Spotify  {mb_fetched} MusicBrainz  {embedded} embedded  {not_found} not found")

# ── Embed art into audio files (so Traktor sees it) ──────────────────────────

def embed_art_in_file(path: str, jpg_data: bytes) -> bool:
    """
    Write JPEG art bytes into the audio file's tags.
    Returns True on success. Skips if art already embedded.
    """
    if not path or not Path(path).exists():
        return False
    ext = Path(path).suffix.lower()
    try:
        if ext in (".mp3", ".aif", ".aiff"):
            from mutagen.id3 import ID3, APIC, error as ID3Error
            try:
                tags = ID3(path)
            except ID3Error:
                tags = ID3()
            # Skip if APIC already present
            if any(k.startswith("APIC") for k in tags):
                return False
            tags.add(APIC(
                encoding=3,          # UTF-8
                mime="image/jpeg",
                type=3,              # Cover (front)
                desc="Cover",
                data=jpg_data,
            ))
            tags.save(path)
            return True
        elif ext in (".mp4", ".m4a"):
            from mutagen.mp4 import MP4, MP4Cover
            tags = MP4(path)
            if tags.get("covr"):
                return False
            tags["covr"] = [MP4Cover(jpg_data, imageformat=MP4Cover.FORMAT_JPEG)]
            tags.save()
            return True
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            tags = FLAC(path)
            if tags.pictures:
                return False
            pic = Picture()
            pic.type     = 3
            pic.mime     = "image/jpeg"
            pic.desc     = "Cover"
            pic.data     = jpg_data
            tags.add_picture(pic)
            tags.save()
            return True
    except Exception as ex:
        print(f"    [embed error] {ex}")
    return False


def embed(limit: int = 0, overwrite: bool = False):
    """
    Embed fetched art into audio files so Traktor can display it.

    Skips files that already have embedded art (unless --overwrite).
    Only embeds tracks that have a confirmed art URL in the index.
    """
    tracks = load_tracks()
    index  = load_json(INDEX_JSON, {})

    todo = []
    for t in tracks:
        dk = t["dkey"]
        url = index.get(dk)
        if not url:
            continue   # null or not fetched
        if not t["path"] or not Path(t["path"]).exists():
            continue
        todo.append(t)

    if limit:
        todo = todo[:limit]

    print(f"Tracks with art to embed: {len(todo):,}")
    if not todo:
        print("Nothing to embed.")
        return

    done = written = skipped = errors = 0

    for i, track in enumerate(todo):
        dk       = track["dkey"]
        art_url  = index.get(dk)
        fname    = art_url.lstrip("/art/") if art_url else ""
        jpg_path = ART_DIR / fname
        if not jpg_path.exists():
            errors += 1
            continue

        jpg_data = jpg_path.read_bytes()
        if len(jpg_data) < 1000:
            errors += 1
            continue

        # Skip files that already have embedded art (unless --overwrite)
        if not overwrite:
            ext = Path(track["path"]).suffix.lower()
            try:
                if ext in (".mp3", ".aif", ".aiff"):
                    from mutagen.id3 import ID3, error as ID3Error
                    try:
                        existing = ID3(track["path"])
                        if any(k.startswith("APIC") for k in existing):
                            skipped += 1
                            done += 1
                            continue
                    except ID3Error:
                        pass
                elif ext in (".mp4", ".m4a"):
                    from mutagen.mp4 import MP4
                    if MP4(track["path"]).get("covr"):
                        skipped += 1
                        done += 1
                        continue
                elif ext == ".flac":
                    from mutagen.flac import FLAC
                    if FLAC(track["path"]).pictures:
                        skipped += 1
                        done += 1
                        continue
            except Exception:
                pass

        ok = embed_art_in_file(track["path"], jpg_data)
        if ok:
            written += 1
            if written % 100 == 0:
                print(f"  [{i+1}/{len(todo)}] embedded {written} so far…")
        else:
            skipped += 1
        done += 1

    print(f"\nEmbed done. {written} written  {skipped} skipped  {errors} errors")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Fetch album art for DJ collection")
    ap.add_argument("--report",    action="store_true")
    ap.add_argument("--run",       action="store_true",  help="Fetch art from Spotify/MB/mutagen")
    ap.add_argument("--embed",     action="store_true",  help="Embed fetched art into audio files")
    ap.add_argument("--limit",     type=int, default=0)
    ap.add_argument("--force",     action="store_true",  help="--run: retry null entries")
    ap.add_argument("--overwrite", action="store_true",  help="--embed: replace existing embedded art")
    args = ap.parse_args()

    if args.report:
        report()
    elif args.run:
        run(limit=args.limit, force=args.force)
    elif args.embed:
        embed(limit=args.limit, overwrite=args.overwrite)
    else:
        ap.print_help()
