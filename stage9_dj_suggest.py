#!/usr/bin/env python3
"""
Stage 9 — DJ Block Planner  (live, floor-reactive)

Watches Deck A and Deck B via Traktor's OSC output. The moment you load
a track, suggestions appear automatically — no typing, no clicking.
Falls back to manual search if OSC is not configured.

Run:   python3 stage9_dj_suggest.py
Open:  http://localhost:7334

── Traktor OSC setup (one-time) ─────────────────────────────────────────────
Traktor Preferences → Controller Manager → Add → Generic OSC

  Device name:   DJ Suggester
  Out-Port:      9000
  Out-IP:        127.0.0.1

Add eight OUT mappings (Type: Output, each one):

  Control: Track > Title        Deck: Deck A   OSC Address: /deck/a/title
  Control: Track > Artist       Deck: Deck A   OSC Address: /deck/a/artist
  Control: Deck > Play          Deck: Deck A   OSC Address: /deck/a/play
  Control: Track > Elapsed Time Deck: Deck A   OSC Address: /deck/a/elapsed_time
  Control: Track > Title        Deck: Deck B   OSC Address: /deck/b/title
  Control: Track > Artist       Deck: Deck B   OSC Address: /deck/b/artist
  Control: Deck > Play          Deck: Deck B   OSC Address: /deck/b/play
  Control: Track > Elapsed Time Deck: Deck B   OSC Address: /deck/b/elapsed_time

The elapsed_time mappings enable karaoke lyrics sync (position in seconds).
Save and close Preferences. Traktor will now broadcast track info here.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import queue
import re
import subprocess
import sys
import termios
import threading
import time
import tty
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Load .env before anything else (python-dotenv optional but recommended)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass  # Flask/Werkzeug will show a tip; env vars already set externally work fine

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE            = Path(__file__).parent
TRAKTOR_NML     = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
SUGGESTIONS_DIR = BASE / "suggestions"
REP_FLAGS_FILE  = BASE / "misc" / "reputation_flags.json"
LYRICS_INDEX    = BASE / "state" / "lyrics_index.json"
ART_INDEX_PATH  = BASE / "state" / "album_art_index.json"
ART_DIR         = BASE / "state" / "album_art"
ACTIVITY_FILE   = BASE / "state" / "activity.json"
PORT            = 7334
OSC_PORT        = 9000

# ── Data model ────────────────────────────────────────────────────────────────

RANKING_TO_STARS = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}

# ── Instrumental detection ────────────────────────────────────────────────────
# Unifies every signal we have today (title patterns, STT theme tag, STT-generated
# "Instrumental — no vocals detected" summary). New signals plug in here and all
# consumers (UI badge, left-border color, future filters) pick them up for free.
_INSTRUMENTAL_TITLE_RE = re.compile(
    r'\b(instrumental|inst\.?|no[ -]?vocals?|karaoke|ambient\s+mix|dub\s+mix)\b',
    re.I,
)
def is_instrumental(title: str, lyric_theme: str | None, lyric_summary: str | None) -> bool:
    if _INSTRUMENTAL_TITLE_RE.search(title or ""):
        return True
    if lyric_theme and lyric_theme.strip().lower() == "instrumental":
        return True
    s = (lyric_summary or "").strip().lower()
    if s.startswith("instrumental") or s.startswith("instrumental —") or s.startswith("[instrumental]"):
        return True
    return False


@dataclass
class Track:
    path:        str
    artist:      str
    title:       str
    bpm:         float
    key:         str
    genre:       str
    stars:       int
    duration:    float      = 0.0   # seconds, from NML PLAYTIME
    comment:     str        = ""    # NML INFO COMMENT — lyric summary (write_nml_comments.py)
    lyric_theme: str        = ""    # NML INFO COMMENT2 — first pipe segment (theme)
    lyric_flags: list       = field(default_factory=list)  # COMMENT2 — ⚑-prefixed flag tokens
    lyrics_full: str        = ""    # NML INFO KEY_LYRICS — full lyrics text (write_nml_lyrics.py)
    lyrics_plain: str       = ""    # LRCLIB plain lyrics from state/lyrics_raw.json (multi-line)
    lyrics_lrc:  str        = ""    # LRC timestamped lyrics from state/lyrics_lrc.json (if available)
    vocal_in_ms: float      = 0.0   # NML CUE_V2 HOTCUE=2 "Vocal In"  (ms)
    vocal_out_ms: float     = 0.0   # NML CUE_V2 HOTCUE=5 "Vocal Out" (ms)
    art_url:     str        = ""    # "/art/{hash}.jpg" from album_art_index.json, or ""

    @property
    def search_text(self) -> str:
        return f"{self.artist} {self.title}".lower()

    def to_dict(self, score: float = 0.0, transition: str = "") -> dict:
        rep   = reputation_for(self.artist)
        sflag = song_flag_for(self.artist, self.title)
        # Metadata now sourced directly from NML fields via load_tracks():
        #   COMMENT      → lyric_summary (one-sentence summary)
        #   COMMENT2     → lyric_theme + lyric_flags (theme | ⚑flag tokens)
        #   KEY_LYRICS   → lyrics_full (full text, for karaoke display)
        lyric_summary = self.comment     or None
        lyric_theme   = self.lyric_theme or None
        return {
            "path":          self.path,
            "artist":        self.artist,
            "title":         self.title,
            "bpm":           round(self.bpm, 1),
            "key":           self.key,
            "genre":         self.genre,
            "stars":         self.stars,
            "score":         round(score * 100),
            "transition":    transition,
            "rep_tier":      rep["tier"]    if rep else None,
            "rep_summary":   rep["summary"] if rep else None,
            "song_flag":     sflag,
            "lyric_summary": lyric_summary,
            "lyric_theme":   lyric_theme,
            "lyric_flags":   self.lyric_flags,
            "lyrics_full":   self.lyrics_full  or None,
            "lyrics_plain":  self.lyrics_plain or None,
            "lyrics_lrc":    self.lyrics_lrc   or None,
            "duration_ms":   round(self.duration * 1000),
            "vocal_in_ms":   round(self.vocal_in_ms)  if self.vocal_in_ms  else None,
            "vocal_out_ms":  round(self.vocal_out_ms) if self.vocal_out_ms else None,
            "art_url":       self.art_url or "",
            "is_instrumental": is_instrumental(self.title, lyric_theme, lyric_summary),
        }


# ── Reputation flags ──────────────────────────────────────────────────────────

def load_reputation_flags(path: Path) -> dict[str, dict]:
    """
    Returns a dict mapping normalised artist-name → {tier, summary, name}.
    Covers both direct artist names and band memberships.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    index: dict[str, dict] = {}
    for flag in data.get("flags", []):
        entry = {"tier": flag["tier"], "summary": flag["summary"], "name": flag["name"]}
        for artist_name in flag.get("artists", []):
            index[artist_name.lower().strip()] = entry
        for band in flag.get("members", []):
            index[band.lower().strip()] = entry
    return index

REP_FLAGS:  dict[str, dict] = load_reputation_flags(REP_FLAGS_FILE)
SONG_FLAGS: dict[str, str]  = {}   # "artist\ttitle" → reason

def _load_song_flags(path: Path) -> dict[str, str]:
    if not path.exists(): return {}
    try:
        data = json.loads(path.read_text())
        return {
            f"{sf['artist'].lower().strip()}\t{sf['title'].lower().strip()}": sf["reason"]
            for sf in data.get("song_flags", [])
        }
    except Exception:
        return {}

SONG_FLAGS = _load_song_flags(REP_FLAGS_FILE)

def reputation_for(artist: str) -> dict | None:
    """Return reputation flag dict if the artist is flagged, else None."""
    return REP_FLAGS.get(artist.lower().strip())

def song_flag_for(artist: str, title: str) -> str | None:
    """Return the reason string if this specific song is flagged, else None."""
    return SONG_FLAGS.get(f"{artist.lower().strip()}\t{title.lower().strip()}")


# ── Lyrics index (DEPRECATED — metadata now sourced from NML fields) ──────────
# Track.comment     ← NML INFO COMMENT   (written by write_nml_comments.py)
# Track.lyric_theme ← NML INFO COMMENT2  (theme segment)
# Track.lyric_flags ← NML INFO COMMENT2  (⚑-flag segments)
# Track.lyrics_full ← NML INFO KEY_LYRICS (written by write_nml_lyrics.py)
#
# lyrics_index.json is no longer loaded at startup. lyrics_for() is retained
# as dead code for one session; remove it next time this file is edited.

LYRICS: dict[str, dict] = {}   # formerly: load_lyrics_index(LYRICS_INDEX)

def lyrics_for(path: str) -> dict | None:
    """DEPRECATED — always returns None now that LYRICS is empty."""
    return None


# ── Album art index ───────────────────────────────────────────────────────────

def _load_art_index(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

ART_INDEX: dict[str, str] = _load_art_index(ART_INDEX_PATH)  # dkey → "/art/{hash}.jpg" | null


# ── LRC (timestamped lyrics) cache ───────────────────────────────────────────

LRC_CACHE_PATH  = BASE / "state" / "lyrics_lrc.json"
PLAIN_CACHE_PATH = BASE / "state" / "lyrics_raw.json"

def _load_lrc_cache(path: Path) -> dict[str, str]:
    """Load artist\ttitle → LRC string mapping. Returns {} if file absent."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

LRC_CACHE:   dict[str, str] = _load_lrc_cache(LRC_CACHE_PATH)
PLAIN_CACHE: dict[str, str] = _load_lrc_cache(PLAIN_CACHE_PATH)  # same key format, plain lyrics


# ── Song key (used for dedup everywhere) ─────────────────────────────────────

def _song_key(t: Track) -> str:
    """Dedup key — same artist+title = same song regardless of file/version."""
    return f"{t.artist.lower().strip()}\t{t.title.lower().strip()}"


# ── NML loader ────────────────────────────────────────────────────────────────

def load_tracks(nml_path: Path) -> list[Track]:
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    raw: list[tuple[int, Track]] = []   # (bitrate, track) — for dedup
    for e in coll.findall("ENTRY"):
        artist = e.get("ARTIST", "").strip()
        title  = e.get("TITLE",  "").strip()
        if not artist and not title:
            continue
        info  = e.find("INFO")
        tempo = e.find("TEMPO")
        loc   = e.find("LOCATION")
        if info is None or loc is None:
            continue
        try:
            bpm = float(tempo.get("BPM", 0)) if tempo is not None else 0.0
        except ValueError:
            bpm = 0.0
        try:
            bitrate = int(info.get("BITRATE", 0) or 0)
        except (ValueError, TypeError):
            bitrate = 0
        ranking = int(info.get("RANKING", 0))
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        try:
            duration = float(info.get("PLAYTIME", 0) or 0)
        except (ValueError, TypeError):
            duration = 0.0
        dk      = f"{artist.lower().strip()}\t{title.lower().strip()}"
        art_url = ART_INDEX.get(dk) or ""
        lrc     = LRC_CACHE.get(dk)   or ""
        plain   = PLAIN_CACHE.get(dk) or ""
        # Fallback: strip trailing parenthetical "(extended ...)" / "(radio edit)" etc.
        if not lrc or not plain:
            base_title = re.sub(r"\s*\([^)]*\)\s*$", "", title.lower().strip()).strip()
            if base_title != title.lower().strip():
                dk2 = f"{artist.lower().strip()}\t{base_title}"
                lrc   = lrc   or LRC_CACHE.get(dk2)   or ""
                plain = plain or PLAIN_CACHE.get(dk2) or ""

        # ── Parse vocal cue points (HOTCUE 2=Vocal In, 5=Vocal Out) ──────────
        vocal_in_ms  = 0.0
        vocal_out_ms = 0.0
        for cue in e.findall("CUE_V2"):
            hc = cue.get("HOTCUE", "-1")
            try:
                start = float(cue.get("START", 0))
            except (ValueError, TypeError):
                start = 0.0
            if hc == "2":    # Vocal In
                vocal_in_ms  = start
            elif hc == "5":  # Vocal Out
                vocal_out_ms = start

        # ── Parse COMMENT2 → theme + lyric_flags ──────────────────────────────
        # Format written by write_nml_comments.py:
        #   theme | ⚑flag1 ⚑flag2 | ⚑rep:tier (name) | ⚑song:reason
        comment2    = info.get("COMMENT2", "") or ""
        lyric_theme = ""
        lyric_flags: list[str] = []
        if comment2:
            c2parts = [p.strip() for p in comment2.split(" | ")]
            start = 0
            if c2parts and not c2parts[0].startswith("⚑"):
                lyric_theme = c2parts[0]
                start = 1
            for part in c2parts[start:]:
                if part.startswith("⚑rep:") or part.startswith("⚑song:"):
                    continue
                # Lyric flags segment: "⚑extreme_violence ⚑sexual_content"
                for tok in part.split():
                    if tok.startswith("⚑"):
                        lyric_flags.append(tok[1:])

        raw.append((bitrate, Track(
            path        = path,
            artist      = artist,
            title       = title,
            bpm         = bpm,
            key         = info.get("KEY",   ""),
            genre       = info.get("GENRE", ""),
            stars       = RANKING_TO_STARS.get(ranking, 0),
            duration    = duration,
            comment     = info.get("COMMENT",   "") or "",
            lyric_theme = lyric_theme,
            lyric_flags = lyric_flags,
            lyrics_full  = info.get("KEY_LYRICS", "") or "",
            lyrics_plain = plain,
            lyrics_lrc   = lrc,
            vocal_in_ms  = vocal_in_ms,
            vocal_out_ms = vocal_out_ms,
            art_url     = art_url,
        )))

    # Deduplicate by artist+title: keep highest-bitrate version.
    # _2.mp3/_3.mp3 etc. are rename-collision duplicates from Stage 2 copy.
    best: dict[str, tuple[int, Track]] = {}
    for bitrate, t in raw:
        key = _song_key(t)
        existing = best.get(key)
        if existing is None or bitrate > existing[0]:
            best[key] = (bitrate, t)

    return [t for _, t in best.values()]


# ── Compatibility scoring ─────────────────────────────────────────────────────

def key_compat(k1: str, k2: str) -> float:
    if not k1 or not k2: return 0.5
    try:
        m1, m2 = k1[-1], k2[-1]
        n1, n2 = int(k1[:-1]), int(k2[:-1])
    except (ValueError, IndexError):
        return 0.5
    if n1 == n2 and m1 == m2: return 1.0
    if n1 == n2:               return 0.9
    diff = min(abs(n1 - n2), 12 - abs(n1 - n2))
    if diff == 1 and m1 == m2: return 0.85
    if diff == 1:              return 0.6
    if diff == 2 and m1 == m2: return 0.4
    if diff <= 3:              return 0.2
    return 0.05


def bpm_compat(b1: float, b2: float) -> float:
    if b1 <= 0 or b2 <= 0: return 0.5
    d = abs(b1 - b2)
    if d <=  2: return 1.0
    if d <=  6: return 0.85
    if d <= 12: return 0.6
    if d <= 18: return 0.3
    if d <= 30: return 0.1
    return 0.0


# Genres that belong in a goth/industrial/darkwave DJ set.
# Tracks outside this set get a score penalty — keeps mainstream pop/disco
# from floating up just because their BPM happens to match.
CORE_GENRES: set[str] = {
    "Gothic Rock", "Darkwave", "Post-Punk", "EBM", "Industrial",
    "New Wave", "Synthpop", "Electronic", "Ambient", "Alternative Rock",
    "Punk", "Hard Rock", "Metal", "Indie Rock", "Rock", "Noise",
    "Shoegaze", "Coldwave", "Neofolk", "Death Rock", "Goth",
    "Witch House", "Minimal Wave", "Power Electronics",
}

# Show-night genre focus (hard filter — tracks outside this set are excluded entirely).
# Set to None to disable and fall back to CORE_GENRES penalty only.
# Future: expose via /api/show-config so this can be set from the browser UI.
#
# Tonight: pure goth/darkwave/industrial — no punk, rock, metal, pop, rap, country.
SHOW_GENRES: set[str] | None = {
    "Gothic Rock", "Darkwave", "Post-Punk", "EBM", "Industrial",
    "New Wave", "Synthpop", "Electronic", "Ambient",
    "Shoegaze", "Coldwave", "Neofolk", "Death Rock", "Goth",
    "Witch House", "Minimal Wave", "Power Electronics",
    "Noise",
}

GENRE_NEIGHBORS: dict[str, list[str]] = {
    "Gothic Rock":      ["Post-Punk", "Darkwave", "New Wave", "Alternative Rock", "Death Rock"],
    "Darkwave":         ["Gothic Rock", "Post-Punk", "Synthpop", "EBM", "Ambient", "Coldwave"],
    "Post-Punk":        ["Gothic Rock", "Darkwave", "New Wave", "Alternative Rock", "Punk"],
    "EBM":              ["Industrial", "Synthpop", "Electronic", "Darkwave"],
    "Industrial":       ["EBM", "Electronic", "Metal", "Hard Rock", "Noise"],
    "New Wave":         ["Synthpop", "Post-Punk", "Gothic Rock"],
    "Synthpop":         ["New Wave", "EBM", "Darkwave", "Electronic"],
    "Electronic":       ["EBM", "Industrial", "Ambient", "Synthpop"],
    "Ambient":          ["Electronic", "Darkwave", "Soundtrack"],
    "Rock":             ["Alternative Rock", "Hard Rock", "Punk"],
    "Alternative Rock": ["Rock", "Indie Rock", "Post-Punk", "Punk"],
    "Indie Rock":       ["Alternative Rock", "Rock", "Post-Punk"],
    "Hard Rock":        ["Rock", "Metal", "Punk"],
    "Punk":             ["Post-Punk", "Alternative Rock", "Hard Rock"],
    "Metal":            ["Hard Rock", "Industrial", "Punk"],
    "Shoegaze":         ["Darkwave", "Post-Punk", "Gothic Rock", "Ambient"],
    "Coldwave":         ["Darkwave", "Post-Punk", "EBM"],
    "Neofolk":          ["Ambient", "Gothic Rock", "Darkwave"],
    "Death Rock":       ["Gothic Rock", "Punk", "Post-Punk"],
    "Noise":            ["Industrial", "Electronic"],
    "Witch House":      ["Darkwave", "Electronic", "Ambient"],
    "Soundtrack":       ["Ambient", "Electronic"],
    "Classical":        ["Ambient", "Soundtrack"],
    "Folk":             ["Indie Rock", "Alternative Rock"],
    "Pop":              [],   # dead end — never bridge to pop
    "Hip-Hop":          [],
    "Comedy":           [],
    "Other":            [],
}


def genre_compat(g1: str, g2: str) -> float:
    if not g1 or not g2: return 0.3
    if g1 == g2:         return 1.0
    nb1 = GENRE_NEIGHBORS.get(g1, [])
    if g2 in nb1:        return 0.6
    for nb in nb1:
        if g2 in GENRE_NEIGHBORS.get(nb, []):
            return 0.3
    return 0.05


# ── Transition type ───────────────────────────────────────────────────────────

def transition_type(src: Track, dst: Track) -> str:
    bpm_d = abs(src.bpm - dst.bpm) if src.bpm > 0 and dst.bpm > 0 else 999
    kc    = key_compat(src.key, dst.key)
    same  = src.genre == dst.genre
    if bpm_d <= 2.5 and same and kc >= 0.8:    return "BEAT+FRAGMENT"
    if bpm_d <= 6   and kc >= 0.8:             return "BEAT MATCH"
    if bpm_d <= 6   and kc < 0.4:              return "BEAT+FX"
    if bpm_d <= 6:                              return "BEAT MATCH"
    if bpm_d <= 12  and kc >= 0.5 and not same: return "STEM BLEND"
    if bpm_d <= 12  and kc >= 0.5:             return "BLEND"
    if kc < 0.25:                              return "EFFECT FADE"
    if bpm_d > 12   and kc >= 0.6:             return "LOOP DROP"
    return "BLEND"


# ── Lyrical theme compatibility ───────────────────────────────────────────────
# Clusters of emotionally adjacent themes — same cluster = full bonus,
# adjacent cluster = half bonus, unrelated = no bonus.
# "surreal" is a wildcard: compatible with everything.
THEME_CLUSTERS: list[set[str]] = [
    {"loss", "isolation", "nostalgia", "alienation"},
    {"love", "loss", "longing"},
    {"anger", "rebellion", "power"},
    {"darkness", "death", "spirituality"},
    {"euphoria", "love"},
    {"identity", "alienation", "isolation"},
]

def theme_compat(t1: str | None, t2: str | None) -> float:
    """1.0 = same theme, 0.5 = adjacent cluster, 0.0 = unrelated. Surreal = 0.5 always."""
    if not t1 or not t2:
        return 0.0          # no data → no effect
    if t1 == t2:
        return 1.0
    if "surreal" in (t1, t2):
        return 0.5
    for cluster in THEME_CLUSTERS:
        if t1 in cluster and t2 in cluster:
            return 1.0      # same cluster
    # Check adjacent clusters (share at least one member in common)
    clusters1 = [c for c in THEME_CLUSTERS if t1 in c]
    clusters2 = [c for c in THEME_CLUSTERS if t2 in c]
    for c1 in clusters1:
        for c2 in clusters2:
            if c1 & c2:     # overlapping clusters → adjacent
                return 0.5
    return 0.0


def _theme(path: str) -> str | None:
    """Return lyric theme for a track path, or None."""
    entry = LYRICS.get(path)
    return entry.get("theme") or None if entry else None


# ── Block suggestions ─────────────────────────────────────────────────────────

def suggest_slot2(anchor: Track, tracks: list[Track], n: int = 8) -> list[dict]:
    anchor_theme    = _theme(anchor.path)
    anchor_artist   = anchor.artist.lower().strip()
    anchor_key      = _song_key(anchor)   # excludes same song regardless of metadata drift
    played_artists  = _get_played_artists()
    best: dict[str, tuple[float, Track]] = {}  # song_key → (score, track)
    for t in tracks:
        if t.path == anchor.path: continue
        if _song_key(t) == anchor_key: continue              # same song (different file/version)
        if t.artist.lower().strip() == anchor_artist: continue   # no same-artist in lock list
        if t.artist.lower().strip() in played_artists: continue  # skip already-played artists
        if SHOW_GENRES is not None and t.genre not in SHOW_GENRES: continue
        gf  = 1.0 if t.genre == anchor.genre else genre_compat(anchor.genre, t.genre) * 0.5
        tc  = theme_compat(anchor_theme, _theme(t.path))
        cg  = 1.0 if t.genre in CORE_GENRES else 0.4   # mainstream penalty
        score = (
            0.33 * bpm_compat(anchor.bpm, t.bpm) +
            0.28 * key_compat(anchor.key, t.key) +
            0.17 * gf +
            0.10 * (t.stars / 5.0) +
            0.07 * cg +
            0.05 * tc
        )
        key = _song_key(t)
        if key not in best or score > best[key][0]:
            best[key] = (score, t)
    # Per-artist dedup: keep only the highest-scoring track per artist in the output
    sorted_results = sorted(best.values(), key=lambda x: -x[0])
    seen_artists: set[str] = set()
    deduped = []
    for s, t in sorted_results:
        a = t.artist.lower().strip()
        if a not in seen_artists:
            seen_artists.add(a)
            deduped.append((s, t))
        if len(deduped) >= n:
            break
    return [t.to_dict(s, transition_type(anchor, t)) for s, t in deduped if s > 0.1]


def suggest_slot3(slot2: Track, anchor: Track, tracks: list[Track]) -> list[dict]:
    genre_filter    = SHOW_GENRES if SHOW_GENRES is not None else CORE_GENRES
    dest_genres     = [g for g in GENRE_NEIGHBORS.get(anchor.genre, []) if g in genre_filter]
    if not dest_genres:
        all_genres  = list({t.genre for t in tracks if t.genre and t.genre in genre_filter})
        dest_genres = [g for g in all_genres if g != anchor.genre][:8]
    exclude         = {anchor.path, slot2.path}
    anchor_artist   = anchor.artist.lower().strip()
    slot2_artist    = slot2.artist.lower().strip()
    anchor_key      = _song_key(anchor)
    slot2_key       = _song_key(slot2)
    played_artists  = _get_played_artists()
    anchor_theme    = _theme(anchor.path)
    groups          = []
    for dest in dest_genres:
        best: dict[str, tuple[float, Track]] = {}
        for t in tracks:
            if t.path in exclude: continue
            if _song_key(t) in {anchor_key, slot2_key}: continue  # same song, different file
            if t.artist.lower().strip() in {anchor_artist, slot2_artist}: continue  # no repeats
            if t.artist.lower().strip() in played_artists: continue
            if SHOW_GENRES is not None and t.genre not in SHOW_GENRES: continue
            mix    = 0.5 * bpm_compat(slot2.bpm, t.bpm) + 0.5 * key_compat(slot2.key, t.key)
            bridge = 1.0 if t.genre == dest else (
                     0.5 if dest in GENRE_NEIGHBORS.get(t.genre, []) else 0.0)
            tc     = theme_compat(anchor_theme, _theme(t.path))
            cg     = 1.0 if t.genre in CORE_GENRES else 0.4
            score  = 0.47 * mix + 0.38 * bridge + 0.10 * cg + 0.05 * tc
            key    = _song_key(t)
            if key not in best or score > best[key][0]:
                best[key] = (score, t)
        candidates = sorted(best.values(), key=lambda x: -x[0])
        # Per-artist dedup within each bridge group
        seen_artists: set[str] = set()
        top = []
        for s, t in candidates:
            a = t.artist.lower().strip()
            if a not in seen_artists and s > 0.25:
                seen_artists.add(a)
                top.append((s, t))
            if len(top) >= 3:
                break
        if top:
            groups.append({"destination": dest,
                           "tracks": [t.to_dict(s, transition_type(slot2, t)) for s, t in top]})
    groups.sort(key=lambda g: -g["tracks"][0]["score"])
    return groups


# ── Playlist file export ──────────────────────────────────────────────────────

def write_m3u(deck: str, anchor: Track, slot2: list[dict], slot3_groups: list[dict]) -> Path:
    """
    Write M3U suggestion playlist for one deck.
    File: suggestions/deck_a.m3u  or  suggestions/deck_b.m3u

    Open the suggestions/ folder in Traktor Explorer to browse live-updated playlists.
    Navigate away and back to refresh.
    """
    SUGGESTIONS_DIR.mkdir(exist_ok=True)
    out = SUGGESTIONS_DIR / f"deck_{deck}.m3u"

    lines = ["#EXTM3U", ""]
    lines.append(f"# Anchor: {anchor.artist} — {anchor.title}  [{anchor.bpm:.1f} BPM | {anchor.key} | {anchor.genre}]")
    lines.append("")

    # ── Slot 2 — Lock ──────────────────────────────────────────────────────────
    lines.append("# ── SLOT 2 · LOCK ────────────────────────────────────────────")
    for t in slot2:
        label = f"{t['artist']} — {t['title']}  [{t['bpm']} BPM | {t['key']} | {t['genre']} | {t['score']}% | {t['transition']}]"
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(t["path"])
    lines.append("")

    # ── Slot 3 — Bridge ────────────────────────────────────────────────────────
    lines.append("# ── SLOT 3 · BRIDGE ──────────────────────────────────────────")
    for group in slot3_groups:
        lines.append(f"# → {group['destination']}")
        for t in group["tracks"]:
            label = f"{t['artist']} — {t['title']}  [{t['bpm']} BPM | {t['key']} | {t['genre']} | {t['score']}% | {t['transition']}]"
            lines.append(f"#EXTINF:-1,{label}")
            lines.append(t["path"])
    lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    return out


# ── Shared suggestion state (Flask thread writes, key listener reads) ─────────

_PRINT_LOCK   = threading.Lock()
_SUGG_LOCK    = threading.Lock()
_SUGG_STATE: dict = {"slot2": [], "slot3": [], "anchor": None}

# Show tracking — paths, artists, and ordered setlist
_PLAYED_LOCK    = threading.Lock()
_PLAYED_PATHS:   set[str]  = set()
_PLAYED_ARTISTS: set[str]  = set()   # normalised lower-strip artist names
_SETLIST:        list[dict] = []      # ordered played tracks {artist,title,genre,bpm,played_at}

# A track is "played" when it has been the SOLE file open in Traktor for at
# least this many seconds after the other deck's file closed.  140s ≈ 2:20 —
# shorter than any track in the collection, longer than any intro preview/loop.
# Previously we used (other_duration + 120) which was backwards and missed
# most tracks.  This simpler rule is both more accurate and easier to reason about.
SOLO_PLAYED_SECS = 140   # seconds a deck must be "solo" before it counts as played

FLOOR_GENRES = {
    "EBM", "Industrial", "Gothic Rock", "Darkwave", "Post-Punk",
    "Synthpop", "Electronic", "New Wave", "Hard Rock", "Metal",
    "Alternative Rock", "Punk", "Dark Electro", "Aggrotech",
    "Noise", "Power Electronics", "Futurepop",
}

def _mark_played(path: str) -> None:
    with _PLAYED_LOCK:
        _PLAYED_PATHS.add(path)

def _mark_played_track(track) -> None:
    """Record a confirmed-played track: path, artist, and setlist entry."""
    with _PLAYED_LOCK:
        _PLAYED_PATHS.add(track.path)
        _PLAYED_ARTISTS.add(track.artist.lower().strip())
        _SETLIST.append({
            "artist":    track.artist,
            "title":     track.title,
            "genre":     track.genre,
            "bpm":       round(track.bpm, 1),
            "played_at": time.strftime("%H:%M"),
        })
        print(f"  [setlist] ✓ played: {track.artist} — {track.title}  [{time.strftime('%H:%M')}]")

def _get_played() -> set[str]:
    with _PLAYED_LOCK:
        return set(_PLAYED_PATHS)

def _get_played_artists() -> set[str]:
    with _PLAYED_LOCK:
        return set(_PLAYED_ARTISTS)

def _get_setlist() -> list[dict]:
    with _PLAYED_LOCK:
        return list(_SETLIST)

def _reset_show() -> None:
    """Clear played paths, artists, and setlist for a fresh show."""
    global _PLAYED_PATHS, _PLAYED_ARTISTS, _SETLIST
    with _PLAYED_LOCK:
        _PLAYED_PATHS   = set()
        _PLAYED_ARTISTS = set()
        _SETLIST        = []
    print("  [setlist] Show reset — played history cleared")


_SUIP_HOOKS: list = []   # callbacks invoked on any state change relevant to SUIP


def _update_sugg_state(slot2: list, slot3: list, anchor) -> None:
    with _SUGG_LOCK:
        _SUGG_STATE["slot2"]  = slot2
        _SUGG_STATE["slot3"]  = slot3
        _SUGG_STATE["anchor"] = anchor
    for h in list(_SUIP_HOOKS):
        try: h()
        except Exception: pass


def _get_sugg_state() -> dict:
    with _SUGG_LOCK:
        return dict(_SUGG_STATE)


# ── Traktor track loader (AppleScript via System Events) ──────────────────────

# Key → slot2 index for each deck
#   Top row  1 2 3 4 5  → Deck A
#   Home row q w e r t  → Deck B
KEYS_DECK_A = {str(i + 1): i for i in range(5)}          # '1'–'5'
KEYS_DECK_B = dict(zip("qwert", range(5)))                # 'q'–'t'

# Keyboard shortcut Traktor uses to load the selected browser track.
#
# ONE-TIME SETUP (do this once in Traktor):
#   Preferences → Controller Manager → Add → Keyboard
#   Add two OUT mappings:
#     Ctrl+1  →  Deck A  →  Load Selected Track
#     Ctrl+2  →  Deck B  →  Load Selected Track
#
# If you map different keys, update LOAD_KEYSTROKE_A / _B here.
LOAD_KEYSTROKE_A = ("1", "control down")   # (character, AppleScript modifier)
LOAD_KEYSTROKE_B = ("2", "control down")


def load_track_in_traktor(deck: str, track: dict) -> None:
    """
    Use macOS System Events to select a track in Traktor's browser, then load it.

    Flow:
      1.  Cmd+F  → open Traktor's search bar
      2.  Type artist + first 4 words of title
      3.  ↓ arrow → highlight first search result
      4.  Ctrl+1 / Ctrl+2 → Traktor loads selected track to Deck A / B
      5.  Esc → close search

    Traktor does NOT need to be in the foreground — System Events targets
    the process directly.  Set TRAKTOR_FOCUS=True below if keystrokes miss.
    """
    TRAKTOR_FOCUS = False   # flip to True if keys aren't landing in Traktor

    artist  = track["artist"].replace('"', "'").replace("\\", "")
    title_w = " ".join(track["title"].split()[:4]).replace('"', "'").replace("\\", "")
    query   = f"{artist} {title_w}"

    char, mod = (LOAD_KEYSTROKE_A if deck == "a" else LOAD_KEYSTROKE_B)

    focus_line = ('tell application "Traktor 4" to activate\n        delay 0.25'
                  if TRAKTOR_FOCUS else "")

    script = f"""
tell application "System Events"
    tell process "Traktor 4"
        {focus_line}
        keystroke "f" using {{command down}}
        delay 0.30
        keystroke "a" using {{command down}}
        delay 0.05
        keystroke "{query}"
        delay 0.50
        key code 125
        delay 0.15
        keystroke "{char}" using {{{mod}}}
        delay 0.10
        key code 53
    end tell
end tell
"""
    subprocess.Popen(["osascript", "-e", script],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── Terminal suggestion output ────────────────────────────────────────────────

# ANSI helpers
_R  = "\033[0m"           # reset
_RED  = "\033[91m"        # bright red   — deck header
_WHT  = "\033[97m"        # bright white — artist/title
_YLW  = "\033[93m"        # yellow       — BPM
_CYN  = "\033[96m"        # cyan         — key
_GRY  = "\033[90m"        # dark grey    — genre / separators
_GLD  = "\033[33m"        # gold         — stars
_GRN  = "\033[92m"        # green        — score / beat match
_MAG  = "\033[95m"        # magenta      — stem blend
_PRP  = "\033[35m"        # purple       — loop drop

# Transition type → (ANSI color, symbol)
TX_STYLE: dict[str, tuple[str, str]] = {
    "BEAT MATCH":    (_GRN, "⚡"),
    "BEAT+FRAGMENT": (_YLW, "✂ "),
    "BEAT+FX":       (_GLD, "🎛"),
    "STEM BLEND":    (_MAG, "≋ "),
    "BLEND":         (_CYN, "〜"),
    "LOOP DROP":     (_PRP, "↺ "),
    "EFFECT FADE":   (_RED, "∿ "),
}

_STARS = {0: "     ", 1: "★    ", 2: "★★   ", 3: "★★★  ", 4: "★★★★ ", 5: "★★★★★"}
_SEP   = _GRY + "─" * 62 + _R
_HDR   = _GRY + "═" * 62 + _R


_TX_WIDTH = 18   # sym(2) + spaces(2) + name(14)
_TX_BLANK = " " * _TX_WIDTH


def _tx(tx: str) -> str:
    """Colored transition symbol + name, padded to _TX_WIDTH visible chars."""
    col, sym = TX_STYLE.get(tx, (_GRY, "  "))
    return f"{col}{sym}  {tx:<14}{_R}"


def _track_line(t: dict, show_tx: bool = True) -> str:
    """Single track row: transition | artist — title | meta | score."""
    tx_str    = _tx(t.get("transition", "")) if show_tx else _TX_BLANK
    stars_str = _GLD + _STARS.get(t["stars"], "     ") + _R
    score_str = _GRN + f"{t['score']:>3}%" + _R
    bpm_str   = _YLW + f"{t['bpm']:>5.1f}" + _R
    key_str   = _CYN + f"{t['key']:<3}" + _R
    gre_str   = _GRY + f"{t['genre'][:14]:<14}" + _R
    artist    = t["artist"][:22]
    title     = t["title"][:26]
    name      = _WHT + f"{artist} — {title}" + _R
    meta      = f"{bpm_str} │ {key_str} │ {gre_str} │ {stars_str}  {score_str}"
    return f"  {tx_str}  {name}\n                    {meta}"


_KEY_A = list("12345")        # load to Deck A
_KEY_B = list("qwert")        # load to Deck B


def print_suggestions(
    deck: str | None,
    anchor: Track,
    slot2: list[dict],
    slot3_groups: list[dict],
) -> None:
    """Clear terminal and print a fresh suggestion block, then update shared state."""
    _update_sugg_state(slot2, slot3_groups, anchor)

    lines: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────────
    deck_tag  = f"{_RED}DECK {deck.upper()} ▶ PLAYING{_R}  " if deck else "  "
    stars_str = _GLD + _STARS.get(anchor.stars, "     ") + _R
    bpm_str   = _YLW + f"{anchor.bpm:.1f}" + _R
    key_str   = _CYN + anchor.key + _R
    gre_str   = _GRY + anchor.genre + _R

    lyr = lyrics_for(anchor.path)
    sflag = song_flag_for(anchor.artist, anchor.title)
    rep   = reputation_for(anchor.artist)

    lines += [
        _HDR,
        f"  {deck_tag}{_WHT}{anchor.artist} — {anchor.title}{_R}",
        f"            {bpm_str} BPM  │  {key_str}  │  {gre_str}  │  {stars_str}",
        _HDR,
        "",
    ]
    if lyr and lyr.get("summary"):
        theme_str = f"  [{lyr['theme']}]" if lyr.get("theme") else ""
        lines.append(f"  {_GRY}♪ {lyr['summary']}{theme_str}{_R}")
        if lyr.get("flags"):
            lines.append(f"  \033[95m⚠ LYRIC FLAGS: {', '.join(lyr['flags'])}{_R}")
    if sflag:
        lines.append(f"  \033[93m⚠ THIS SONG: {sflag}{_R}")
    if rep:
        tier_col = "\033[91m" if rep["tier"]=="convicted" else ("\033[92m" if rep["tier"]=="settled" else "\033[93m")
        lines.append(f"  {tier_col}⚠ ARTIST ({rep['tier'].upper()}): {rep['summary']}{_R}")
    if lyr or sflag or rep:
        lines.append("")

    # ── Slot 2 — Lock ────────────────────────────────────────────────────────
    lines.append(f"  {_WHT}LOCK — PLAY NEXT{_R}  "
                 f"{_GRY}[1–5 = Deck A   q–t = Deck B]{_R}\n")
    for i, t in enumerate(slot2[:5]):
        ka = _GRN + _KEY_A[i] + _R
        kb = _GRY + _KEY_B[i] + _R
        key_label = f"{_GRY}[{_R}{ka}{_GRY}/{_R}{kb}{_GRY}]{_R}"
        lines.append(f"  {key_label} {_track_line(t)}")
        lines.append("")

    # ── Slot 3 — Bridge ──────────────────────────────────────────────────────
    if slot3_groups:
        lines.append(f"  {_WHT}BRIDGE — AFTER THAT{_R}\n")
        for group in slot3_groups[:3]:
            lines.append(f"  {_GRY}→ {group['destination']}{_R}")
            for t in group["tracks"][:2]:
                lines.append(f"       {_track_line(t, show_tx=True)}")
                lines.append("")

    lines.append(_SEP)

    with _PRINT_LOCK:
        sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
        sys.stdout.flush()


# ── Interactive key listener ──────────────────────────────────────────────────

def run_key_listener() -> None:
    """
    Run on the main thread.  Reads single keypresses and loads suggestions into
    Traktor without leaving the terminal.

    Key map:
      1 2 3 4 5  → Load Lock suggestion N  to Deck A
      q w e r t  → Load Lock suggestion N  to Deck B
      x / Ctrl+C → quit

    Requires one-time Traktor setup (see LOAD_KEYSTROKE_A / _B at top of file).
    """
    import os

    # Only run if stdin is a real terminal (not piped)
    if not sys.stdin.isatty():
        threading.Event().wait()   # block forever, let daemon threads run
        return

    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def _msg(text: str) -> None:
        with _PRINT_LOCK:
            sys.stdout.write(f"\n  {text}\n")
            sys.stdout.flush()

    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)

            # Quit keys
            if ch in ("\x03", "\x04", "x", "X"):
                _msg("Bye.")
                os._exit(0)

            state = _get_sugg_state()
            slot2 = state["slot2"]

            if ch in KEYS_DECK_A:
                idx = KEYS_DECK_A[ch]
                if idx < len(slot2):
                    t = slot2[idx]
                    _msg(f"→ Deck A: {t['artist']} — {t['title']}")
                    threading.Thread(target=load_track_in_traktor,
                                     args=("a", t), daemon=True).start()

            elif ch in KEYS_DECK_B:
                idx = KEYS_DECK_B[ch]
                if idx < len(slot2):
                    t = slot2[idx]
                    _msg(f"→ Deck B: {t['artist']} — {t['title']}")
                    threading.Thread(target=load_track_in_traktor,
                                     args=("b", t), daemon=True).start()

    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── lsof deck watcher ────────────────────────────────────────────────────────

AUDIO_EXTS = {".mp3", ".flac", ".aiff", ".aif", ".wav", ".m4a", ".ogg"}
TRAKTOR_PROC = "Traktor Pro 4"


def _traktor_open_audio() -> list[tuple[int, str]]:
    """
    Return list of (fd, abs_path) for audio files open in Traktor,
    sorted by file descriptor (lower fd = opened earlier = likely Deck A).
    """
    try:
        pid = subprocess.check_output(
            ["pgrep", "-x", TRAKTOR_PROC], text=True
        ).strip().split()[0]
    except Exception:
        return []
    try:
        out = subprocess.check_output(
            ["lsof", "-p", pid], text=True, stderr=subprocess.DEVNULL
        )
    except Exception:
        return []
    results = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        fd_raw = parts[3]   # e.g. "24r"
        path   = " ".join(parts[8:])
        if Path(path).suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            fd = int("".join(c for c in fd_raw if c.isdigit()))
        except ValueError:
            fd = 9999
        results.append((fd, path))
    results.sort()   # ascending fd → Deck A first
    return results


def start_lsof_watcher(
    tracks: list,
    index: dict,
    osc_state,
    interval: float = 2.0,
) -> None:
    """
    Poll lsof every `interval` seconds.  When Traktor opens a new audio file,
    resolve it against our collection and push deck suggestions via SSE.

    Deck assignment: lower file-descriptor number → Deck A, higher → Deck B.
    This matches Traktor's load order in practice.

    Played detection (time-based):
      When a file leaves a deck, check how long it was open.
      Once the other deck's file closes, a solo timer starts. If the remaining
      deck's file stays open for SOLO_PLAYED_SECS, it was definitely played.
      Confirmed-played tracks land in _SETLIST for the post-show export.
    """
    # path → deck letter
    deck_map:       dict[str, str]   = {}
    # path → time.time() when first seen by lsof
    load_times:     dict[str, float] = {}
    # deck → Track currently in that deck
    deck_track:     dict[str, object] = {}
    # deck → timestamp when it became the SOLE active deck (other deck closed its file)
    # Once (now - solo_since) >= SOLO_PLAYED_SECS, the deck's track counts as played.
    deck_solo_since: dict[str, float] = {}

    def _resolve(fpath: str):
        track = index.get(fpath)
        if track is None:
            bn    = Path(fpath).name
            track = next((t for t in tracks if Path(t.path).name == bn), None)
        return track

    def _on_file_left(fpath: str, deck: str) -> None:
        """Called when a file disappears from a deck.

        If the other deck still has a file open, start its solo timer now.
        That means: once SOLO_PLAYED_SECS pass with that file still open,
        the track was definitely played (not just previewed).
        """
        load_times.pop(fpath, None)
        other_deck = "b" if deck == "a" else "a"
        # Check if the other deck has a file currently open
        other_path = next((p for p, d in deck_map.items() if d == other_deck), None)
        if other_path and other_path not in deck_solo_since:
            deck_solo_since[other_deck] = time.time()
            print(f"  [lsof] Deck {deck.upper()} released — solo timer started for Deck {other_deck.upper()}")

    def _loop():
        nonlocal deck_map

        # Bootstrap: don't fire suggestions for already-in-deck tracks,
        # but DO populate deck cards and mark as loaded.
        boot = _traktor_open_audio()
        deck_slots = ["a", "b"]
        now = time.time()
        for i, (_, fpath) in enumerate(boot[:2]):
            d = deck_slots[i] if i < len(deck_slots) else "a"
            deck_map[fpath]  = d
            load_times[fpath] = now
            _mark_played(fpath)
            track = _resolve(fpath)
            if track:
                deck_track[d] = track
                osc_state.push_track(track, d)
                try:
                    s2  = suggest_slot2(track, tracks)
                    ref = index.get(s2[0]["path"]) if s2 else track
                    s3  = suggest_slot3(ref, track, tracks)
                    print_suggestions(d, track, s2, s3)
                except Exception:
                    pass

        while True:
            time.sleep(interval)
            current_list  = _traktor_open_audio()
            current_paths = {p for _, p in current_list}
            prev_paths    = set(deck_map.keys())
            new_files     = current_paths - prev_paths
            gone_files    = prev_paths    - current_paths

            # Check departing files — start solo timers for surviving deck
            for fpath in gone_files:
                deck = deck_map.get(fpath)
                if deck:
                    _on_file_left(fpath, deck)

            # Check solo timers — mark played if threshold reached
            now = time.time()
            for deck, solo_start in list(deck_solo_since.items()):
                if (now - solo_start) >= SOLO_PLAYED_SECS:
                    solo_path = next((p for p, d in deck_map.items() if d == deck), None)
                    if solo_path and solo_path in current_paths:
                        track = _resolve(solo_path)
                        if track:
                            _mark_played_track(track)
                    deck_solo_since.pop(deck, None)

            if not new_files and not gone_files:
                continue

            # Reclaim deck slots freed by closed files
            freed_decks = [deck_map.pop(fp) for fp in gone_files if fp in deck_map]
            freed_decks.sort()   # "a" before "b"

            if not new_files:
                continue

            # Assign new files: reuse freed slots first, then by fd order
            new_sorted = [(fd, p) for fd, p in current_list if p in new_files]
            new_sorted.sort()

            for i, (_, fpath) in enumerate(new_sorted):
                if freed_decks:
                    d = freed_decks.pop(0)
                elif len(deck_map) == 0:
                    d = "a"
                else:
                    existing = set(deck_map.values())
                    d = "b" if "a" in existing else "a"
                deck_map[fpath]   = d
                load_times[fpath] = time.time()

                track = _resolve(fpath)
                if not track:
                    continue

                deck_track[d] = track
                _mark_played(fpath)
                osc_state.push_track(track, d)
                try:
                    s2  = suggest_slot2(track, tracks)
                    ref = index.get(s2[0]["path"]) if s2 else track
                    s3  = suggest_slot3(ref, track, tracks)
                    print_suggestions(d, track, s2, s3)
                except Exception:
                    pass

    t = threading.Thread(target=_loop, daemon=True, name="lsof-watcher")
    t.start()


# ── OSC state ─────────────────────────────────────────────────────────────────

class OSCState:
    """Thread-safe buffer for Traktor OSC deck events."""
    def __init__(self):
        self._lock    = threading.Lock()
        self._pending = {}        # deck → {title, artist}  (accumulates until both arrive)
        self._loaded  = {}        # deck → {title, artist}  (last fully-loaded track)
        self._playing = {}        # deck → bool
        self._elapsed = {}        # deck → float seconds (playback position)
        self._sse_qs  = []        # SSE client queues

    def _push(self, event: dict, suip: bool = True) -> None:
        """Send an event to all connected SSE clients (must hold lock).

        suip=False suppresses SUIP hook firing — use for high-frequency events
        like position ticks that don't change the scene tree.
        """
        for q in list(self._sse_qs):
            try: q.put_nowait(event)
            except: pass
        if suip:
            # Fire SUIP hooks — client-side coalescer caps at 10/sec.
            for h in list(_SUIP_HOOKS):
                try: h()
                except Exception: pass

    def get_elapsed(self, deck: str) -> float:
        """Return playback position in seconds for a deck (0.0 if unknown)."""
        with self._lock:
            return self._elapsed.get(deck, 0.0)

    def on_message(self, deck: str, field: str, value: str):
        with self._lock:
            # ── Playback position (high-frequency — no SUIP trigger) ──────────
            if field == "elapsed_time":
                try:
                    self._elapsed[deck] = float(value)
                except (ValueError, TypeError):
                    pass
                self._push({"type": "position", "deck": deck,
                            "elapsed": self._elapsed.get(deck, 0.0)},
                           suip=False)
                return

            # ── Play-state change ─────────────────────────────────────────────
            if field == "play":
                playing = (str(value).strip() in ("1", "1.0", "True", "true"))
                was_playing = self._playing.get(deck, False)
                self._playing[deck] = playing
                # Fire play_state event so browser can show ▶ on the right deck
                self._push({"type": "play_state", "deck": deck, "playing": playing})
                # When a deck starts playing (0→1) and we have its loaded track,
                # fire a track event so suggestions auto-update for the live deck
                if playing and not was_playing and deck in self._loaded:
                    info = self._loaded[deck]
                    self._push({"deck": deck,
                                "title": info["title"],
                                "artist": info["artist"],
                                "type": "playing"})
                return

            # ── Track load (title + artist accumulate) ────────────────────────
            p = self._pending.setdefault(deck, {})
            p[field] = value.strip()
            if "title" in p and "artist" in p:
                info  = {"title": p["title"], "artist": p["artist"]}
                self._loaded[deck] = info
                self._pending[deck] = {}
                self._push({"deck": deck, "title": info["title"],
                            "artist": info["artist"], "type": "loaded"})

    def playing_deck(self) -> str | None:
        """Return the deck letter currently playing, or None."""
        with self._lock:
            for deck, playing in self._playing.items():
                if playing:
                    return deck
        return None

    def get_loaded(self) -> dict:
        """Return a copy of loaded deck state: {deck: {title, artist}}."""
        with self._lock:
            return dict(self._loaded)

    def get_playing(self) -> dict:
        """Return a copy of play state: {deck: bool}."""
        with self._lock:
            return dict(self._playing)

    def push_track(self, track, deck: str | None) -> None:
        """Called by lsof watcher when a new track is detected in Traktor."""
        with self._lock:
            info = {"title": track.title, "artist": track.artist}
            if deck:
                self._loaded[deck] = info
            self._push({
                "type":   "loaded",
                "deck":   deck or "a",
                "title":  track.title,
                "artist": track.artist,
            })

    def swap_decks(self) -> None:
        """Swap deck A ↔ B assignments and notify all SSE clients."""
        with self._lock:
            a = self._loaded.get("a")
            b = self._loaded.get("b")
            if a: self._loaded["b"] = a
            if b: self._loaded["a"] = b
            elif a: del self._loaded["b"]
            # Notify browser to re-render both deck cards
            if a: self._push({"type": "loaded", "deck": "b",
                               "title": a["title"], "artist": a["artist"]})
            if b: self._push({"type": "loaded", "deck": "a",
                               "title": b["title"], "artist": b["artist"]})

    def broadcast_input(self, text: str) -> None:
        """Inject text into every browser's search box via SSE.
        Used by external DJ services (OCR, voice-to-text, remote control)."""
        with self._lock:
            self._push({"type": "input_text", "text": text})

    def add_client(self, q):
        with self._lock: self._sse_qs.append(q)

    def remove_client(self, q):
        with self._lock:
            try: self._sse_qs.remove(q)
            except ValueError: pass


def start_osc_server(state: OSCState, port: int) -> bool:
    """Start UDP OSC listener. Returns True if started, False if unavailable."""
    try:
        from pythonosc import dispatcher as osc_dispatcher
        from pythonosc.osc_server import ThreadingOSCUDPServer
    except ImportError:
        return False

    d = osc_dispatcher.Dispatcher()

    def handler(address, *args):
        # address like /deck/a/title or /deck/b/artist
        parts = address.strip("/").split("/")   # ['deck','a','title']
        if len(parts) == 3:
            _, deck, field = parts
            state.on_message(deck, field, str(args[0]) if args else "")

    d.set_default_handler(handler)

    try:
        server = ThreadingOSCUDPServer(("127.0.0.1", port), d)
    except OSError:
        return False

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return True


# ── Traktor history watcher (fallback when OSC unavailable) ──────────────────
#
# Traktor Pro 4 removed the Generic OSC device type from Controller Manager.
# As a fallback we poll the Traktor history NML, which Traktor appends to
# every time a track is loaded onto a deck.  We call osc_state.on_message()
# with the track info, simulating exactly what the OSC handler would deliver.
# Position is driven by wall-clock elapsed time since detection — close enough
# for estimated-timing karaoke and live suggestions.

class HistoryWatcher(threading.Thread):
    HISTORY_DIR = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/History"
    POLL_SECS   = 1.0
    POSITION_HZ = 5        # position ticks per second while a deck is playing

    def __init__(self, osc_state: "OSCState", track_index: dict):
        super().__init__(daemon=True, name="HistoryWatcher")
        self._osc        = osc_state
        self._index      = track_index   # abs path → Track
        self._stop_ev    = threading.Event()
        self._deck_start: dict[str, float] = {}   # "0"/"1" → wall clock when detected

    def stop(self):
        self._stop_ev.set()

    def _latest_file(self) -> Path | None:
        try:
            files = sorted(self.HISTORY_DIR.glob("history_*.nml"))
            return files[-1] if files else None
        except Exception:
            return None

    def _parse_entries(self, path: Path) -> list[dict]:
        try:
            tree = ET.parse(path)
            out  = []
            for entry in tree.getroot().iter("ENTRY"):
                pk = entry.find("PRIMARYKEY")
                ex = entry.find("EXTENDEDDATA")
                if pk is None or ex is None:
                    continue
                out.append({
                    "key":   pk.get("KEY", ""),
                    "deck":  ex.get("DECK", "0"),
                    "start": ex.get("STARTTIME", "0"),
                })
            return out
        except Exception:
            return []

    @staticmethod
    def _key_to_path(key: str) -> str:
        """'Macintosh HD/:Users/:…' → '/Users/…'"""
        idx = key.find("/:")
        return key[idx:].replace("/:", "/") if idx >= 0 else key

    def _push_track(self, deck_num: str, key: str):
        deck = "a" if deck_num == "0" else "b"
        path = self._key_to_path(key)
        t    = self._index.get(path)
        if t:
            self._osc.on_message(deck, "title",  t.title)
            self._osc.on_message(deck, "artist", t.artist)
        else:
            parts  = [p for p in path.split("/") if p]
            title  = Path(parts[-1]).stem if parts          else "Unknown"
            artist = parts[-3]            if len(parts) >= 3 else ""
            self._osc.on_message(deck, "title",  title)
            self._osc.on_message(deck, "artist", artist)
        self._osc.on_message(deck, "play", "1")
        self._deck_start[deck_num] = time.time()

    def run(self):
        hist_file:  Path | None = None
        last_mtime: float       = 0.0
        seen_ids:   set[str]    = set()
        last_tick:  float       = time.time()

        while not self._stop_ev.is_set():
            now = time.time()

            # ── Position ticks for playing decks ───────────────────────────
            if now - last_tick >= 1.0 / self.POSITION_HZ:
                last_tick = now
                for deck_num, wall_start in list(self._deck_start.items()):
                    deck = "a" if deck_num == "0" else "b"
                    self._osc.on_message(deck, "elapsed_time",
                                         str(now - wall_start))

            # ── Detect new Traktor session (new history file) ──────────────
            new_file = self._latest_file()
            if new_file != hist_file:
                hist_file  = new_file
                last_mtime = 0.0
                seen_ids   = set()

            if not hist_file:
                time.sleep(self.POLL_SECS)
                continue

            try:
                mtime = hist_file.stat().st_mtime
            except OSError:
                time.sleep(self.POLL_SECS)
                continue

            if mtime != last_mtime:
                last_mtime = mtime
                entries    = self._parse_entries(hist_file)

                if not seen_ids:
                    # First read — mark all existing as seen so we don't
                    # replay history.  Only pre-populate deck state if the
                    # history file is fresh (< 4 hours old) — avoids pushing
                    # stale days-old tracks as "currently playing".
                    file_age = time.time() - mtime
                    fresh    = file_age < 4 * 3600
                    latest: dict[str, dict] = {}
                    for e in entries:
                        latest[e["deck"]] = e
                        seen_ids.add(f"{e['deck']}:{e['key']}:{e['start']}")
                    if fresh:
                        for e in latest.values():
                            self._push_track(e["deck"], e["key"])
                else:
                    for e in entries:
                        uid = f"{e['deck']}:{e['key']}:{e['start']}"
                        if uid not in seen_ids:
                            seen_ids.add(uid)
                            self._push_track(e["deck"], e["key"])

            time.sleep(self.POLL_SECS)


# ── Flask app ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac Bilby</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=Oswald:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
/* ══ Theme variables ══════════════════════════════════════════════════════════ */
/* Night (default) */
:root{
  --bg:#111;--bg2:#0d0d1a;--bg3:#0a0a0a;--bg4:#1a1a1a;
  --border:#1a1a1a;--border2:#2d2d2d;
  --text:#ddd;--text2:#888;--text3:#555;--text4:#444;
  --accent:#e63946;--col1:#e63946;--col2:#f4a261;--col3:#4cc9f0;
  --card-bg:#161616;--card-sel:#1a1a2e;
  --meta:#666;--lyric:#9aa5b8;
  --bpm:#f4a261;--key:#a8dadc;--gen:#aaa;--scr:#4a9;
  --anchor-bg:#1a0808;--anchor-bdr:#e63946;--anchor-col:#e63946;
  --dc-play-bdr:#e63946;--dc-play-bg:#1a0808;--dc-play-glow:#e6394633;
  --srch-bg:#161616;--inp-bg:#1e1e1e;--inp-bdr:#333;--res-bg:#1a1a1a;
  --hdr-bg:#0d0d1a;--hdr-bdr:#e63946;--hdr-text:#e63946;--hdr-sub:#888;
  --deck-bg:#0a0a0a;--deck-bdr:#1a1a1a;
  --pill-bdr:#222;--pill-text:#444;--pill-loaded-bg:#1a1a1a;
  --swap-bg:#1a1a1a;--swap-bdr:#333;--swap-text:#777;
  --save-bg:#7f1d1d;--save-text:#fca5a5;
  --surp-bg:#1e3a5f;--surp-text:#93c5fd;
  --show-bg:#1a1a1a;--show-bdr:#3b2d6e;--show-text:#a78bfa;
  --show-f-bg:#1e1b40;--show-f-bdr:#7c3aed;--show-f-text:#c4b5fd;
  --sl-bg:#1a1a1a;--sl-bdr:#065f46;--sl-text:#6ee7b7;
  --sl-on-bg:#064e3b;--sl-on-bdr:#34d399;--sl-on-text:#6ee7b7;
  --rst-bg:#1a1a1a;--rst-bdr:#7f1d1d;--rst-text:#f87171;
  --act-col:#4cc9f0;
  --tx-beat:#14532d;--tx-beat-t:#4ade80;
  --tx-frag:#713f12;--tx-frag-t:#facc15;
  --tx-fx:#7c2d12;--tx-fx-t:#fb923c;
  --tx-blend:#164e63;--tx-blend-t:#a8dadc;
  --tx-stem:#701a75;--tx-stem-t:#e879f9;
  --tx-loop:#4a1d96;--tx-loop-t:#c084fc;
  --tx-efx:#7f1d1d;--tx-efx-t:#f87171;
  --tx-cut:#1e293b;--tx-cut-t:#94a3b8;
  --btn-r:3px;--lbl-font:inherit;
}
/* Day (outdoor) */
body.day{
  --bg:#f0ede8;--bg2:#e8e4de;--bg3:#e2ddd7;--bg4:#d8d3cc;
  --border:#c8c3bc;--border2:#b8b3ac;
  --text:#1a1a1a;--text2:#444;--text3:#666;--text4:#888;
  --accent:#CC7700;--col1:#CC7700;--col2:#BB9900;--col3:#6677AA;
  --card-bg:#ebe7e1;--card-sel:#fff0cc;
  --meta:#555;--lyric:#4a5568;
  --bpm:#b85a00;--key:#1a6080;--gen:#555;--scr:#2a7a3a;
  --anchor-bg:#fff5e0;--anchor-bdr:#CC7700;--anchor-col:#CC7700;
  --dc-play-bdr:#CC7700;--dc-play-bg:#fff5e0;--dc-play-glow:#CC770033;
  --srch-bg:#e8e4de;--inp-bg:#f5f2ee;--inp-bdr:#b8b3ac;--res-bg:#ebe7e1;
  --hdr-bg:#e8e4de;--hdr-bdr:#CC7700;--hdr-text:#CC7700;--hdr-sub:#666;
  --deck-bg:#ddd8d0;--deck-bdr:#b8b3ac;
  --pill-bdr:#b8b3ac;--pill-text:#666;--pill-loaded-bg:#d8d3cc;
  --swap-bg:#d8d3cc;--swap-bdr:#b8b3ac;--swap-text:#444;
  --save-bg:#b91c1c;--save-text:#fee2e2;
  --surp-bg:#1e40af;--surp-text:#bfdbfe;
  --show-bg:#e2ddd7;--show-bdr:#7c3aed;--show-text:#6d28d9;
  --show-f-bg:#ede9fe;--show-f-bdr:#7c3aed;--show-f-text:#4c1d95;
  --sl-bg:#e2ddd7;--sl-bdr:#065f46;--sl-text:#065f46;
  --sl-on-bg:#d1fae5;--sl-on-bdr:#059669;--sl-on-text:#065f46;
  --rst-bg:#e2ddd7;--rst-bdr:#b91c1c;--rst-text:#b91c1c;
  --act-col:#1a6080;
}
/* LCARS (Star Trek TNG) */
body.lcars{
  --bg:#000;--bg2:#060606;--bg3:#040404;--bg4:#0a0a0a;
  --border:#111;--border2:#1a1a1a;
  --text:#FFCC99;--text2:#AA8855;--text3:#664422;--text4:#332211;
  --accent:#FF9900;--col1:#FF9900;--col2:#FFCC00;--col3:#9999CC;
  --card-bg:#050505;--card-sel:#1a1200;
  --meta:#776655;--lyric:#aa9977;
  --bpm:#FF9900;--key:#9999CC;--gen:#887766;--scr:#66CC66;
  --anchor-bg:#0f0800;--anchor-bdr:#FF9900;--anchor-col:#FF9900;
  --dc-play-bdr:#FF9900;--dc-play-bg:#0f0800;--dc-play-glow:#FF990033;
  --srch-bg:#050505;--inp-bg:#0a0a0a;--inp-bdr:#221100;--res-bg:#070707;
  --hdr-bg:#FF9900;--hdr-bdr:#FF9900;--hdr-text:#000;--hdr-sub:#4a2800;
  --deck-bg:#060606;--deck-bdr:#FF9900;
  --pill-bdr:#331100;--pill-text:#664422;--pill-loaded-bg:#0f0800;
  --swap-bg:#111;--swap-bdr:#333;--swap-text:#886644;
  --save-bg:#FF3300;--save-text:#fff;
  --surp-bg:#334499;--surp-text:#99CCFF;
  --show-bg:#221144;--show-bdr:#9966CC;--show-text:#CC99FF;
  --show-f-bg:#331166;--show-f-bdr:#CC99FF;--show-f-text:#fff;
  --sl-bg:#003322;--sl-bdr:#00CC66;--sl-text:#00FF88;
  --sl-on-bg:#005533;--sl-on-bdr:#00FF88;--sl-on-text:#00FFAA;
  --rst-bg:#330000;--rst-bdr:#FF3300;--rst-text:#FF6666;
  --act-col:#FF9900;
  --btn-r:20px;--lbl-font:'Oswald',sans-serif;
}
/* Borg */
body.borg{
  --bg:#000;--bg2:#000305;--bg3:#000;--bg4:#010a01;
  --border:#001500;--border2:#002800;
  --text:#00CC00;--text2:#007700;--text3:#004400;--text4:#002200;
  --accent:#00FF00;--col1:#00FF00;--col2:#00BB00;--col3:#00AAAA;
  --card-bg:#000;--card-sel:#001a00;
  --meta:#005500;--lyric:#00aa55;
  --bpm:#00FF00;--key:#00AAAA;--gen:#006600;--scr:#00CC44;
  --anchor-bg:#001500;--anchor-bdr:#00FF00;--anchor-col:#00FF00;
  --dc-play-bdr:#00FF00;--dc-play-bg:#001500;--dc-play-glow:#00FF0022;
  --srch-bg:#000;--inp-bg:#000;--inp-bdr:#003300;--res-bg:#000805;
  --hdr-bg:#000;--hdr-bdr:#00FF00;--hdr-text:#00FF00;--hdr-sub:#006600;
  --deck-bg:#000;--deck-bdr:#003300;
  --pill-bdr:#002200;--pill-text:#004400;--pill-loaded-bg:#001500;
  --swap-bg:#000;--swap-bdr:#003300;--swap-text:#006600;
  --save-bg:#003300;--save-text:#00FF00;
  --surp-bg:#003333;--surp-text:#00FFFF;
  --show-bg:#000;--show-bdr:#003300;--show-text:#00AA00;
  --show-f-bg:#001a00;--show-f-bdr:#00FF00;--show-f-text:#00FF00;
  --sl-bg:#000;--sl-bdr:#003300;--sl-text:#00AA00;
  --sl-on-bg:#001a00;--sl-on-bdr:#00FF00;--sl-on-text:#00FF00;
  --rst-bg:#000;--rst-bdr:#003300;--rst-text:#006600;
  --act-col:#00FF00;
  --btn-r:0px;--lbl-font:'Courier New',monospace;
}
body.passthrough{
  --bg:#000;--bg2:#000;--bg3:#000;--bg4:#000;
  --border:transparent;--border2:#003300;
  --text:#FFFFFF;--text2:#00FF00;--text3:#66FF66;--text4:#003300;
  --accent:#00FF00;--col1:#00FF00;--col2:#00FF00;--col3:#00FF00;
  --card-bg:#000;--card-sel:#002200;
  --meta:#88FF88;--lyric:#88FF88;
  --bpm:#FFCC00;--key:#00CCFF;--gen:#888;--scr:#00FF00;
  --anchor-bg:#000;--anchor-bdr:transparent;--anchor-col:#00FF00;
  --dc-play-bdr:#00FF00;--dc-play-bg:#000;--dc-play-glow:#00FF0033;
  --srch-bg:#000;--inp-bg:#000;--inp-bdr:#003300;--res-bg:#000;
  --hdr-bg:#000;--hdr-bdr:transparent;--hdr-text:#00FF00;--hdr-sub:#004400;
  --deck-bg:#000;--deck-bdr:transparent;
  --pill-bdr:#003300;--pill-text:#006600;--pill-loaded-bg:#000;
  --swap-bg:#000;--swap-bdr:#003300;--swap-text:#00FF00;
  --save-bg:#002200;--save-text:#00FF00;
  --surp-bg:#002233;--surp-text:#00FFFF;
  --show-bg:#000;--show-bdr:#003300;--show-text:#00AA00;
  --show-f-bg:#001a00;--show-f-bdr:#00FF00;--show-f-text:#00FF00;
  --sl-bg:#000;--sl-bdr:#003300;--sl-text:#00AA00;
  --sl-on-bg:#001a00;--sl-on-bdr:#00FF00;--sl-on-text:#00FF00;
  --rst-bg:#000;--rst-bdr:#003300;--rst-text:#006600;
  --act-col:#00FF00;
  --btn-r:3px;--lbl-font:inherit;
}
/* ══ Base elements (all themes via CSS vars) ══════════════════════════════════ */
body{background:var(--bg);color:var(--text);font-family:'Atkinson Hyperlegible','Courier New',monospace;font-size:13px;height:100vh;display:flex;flex-direction:column;transition:background .2s,color .2s}
#hdr{background:var(--hdr-bg);padding:10px 18px;border-bottom:2px solid var(--hdr-bdr);display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{color:var(--hdr-text);font-family:var(--lbl-font);font-size:15px;letter-spacing:3px;text-transform:uppercase;flex:1}
#hdr small{color:var(--hdr-sub);font-size:11px}
#theme-btn,#art-reload-btn,#stt-btn{background:transparent;border:1px solid var(--border2);color:var(--text2);padding:3px 10px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:12px;cursor:pointer;letter-spacing:1px;flex-shrink:0;transition:all .15s;text-transform:uppercase}
#theme-btn:hover,#art-reload-btn:hover,#stt-btn:hover{border-color:var(--text2);color:var(--text)}
#art-reload-btn,#stt-btn{font-size:14px;padding:3px 7px}
#stt-btn.listening{color:#ff3344;border-color:#ff3344;animation:sttPulse 1.2s ease-in-out infinite}
@keyframes sttPulse{0%,100%{box-shadow:0 0 0 0 rgba(255,51,68,0.6)}50%{box-shadow:0 0 0 6px rgba(255,51,68,0)}}
#osc-status{font-size:10px;padding:3px 9px;border-radius:3px;letter-spacing:1px;text-transform:uppercase}
#osc-status.on{background:#14532d;color:#4ade80}
#osc-status.off{background:#1e293b;color:#555}
#deck-bar{background:var(--deck-bg);border-bottom:1px solid var(--deck-bdr);padding:6px 18px;display:flex;gap:10px;align-items:center;flex-shrink:0;min-height:34px;flex-wrap:wrap}
.deck-pill{font-size:10px;padding:3px 10px;border-radius:var(--btn-r);letter-spacing:1px;text-transform:uppercase;border:1px solid var(--pill-bdr);color:var(--pill-text);font-family:var(--lbl-font)}
.deck-pill.loaded{border-color:#555;color:#888;background:var(--pill-loaded-bg)}
.deck-pill.playing{border-color:var(--accent);color:var(--accent);background:var(--anchor-bg)}
#deck-msg{color:var(--text3);font-size:11px;flex:1}
#swap-btn{background:var(--swap-bg);color:var(--swap-text);border:1px solid var(--swap-bdr);padding:3px 10px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;letter-spacing:1px;text-transform:uppercase;flex-shrink:0}
#swap-btn:hover{border-color:var(--text2);color:var(--text)}
.panic-btn{border:none;padding:5px 13px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px;transition:opacity .1s;text-transform:uppercase;flex-shrink:0}
#save-btn{background:var(--save-bg);color:var(--save-text)}
#save-btn:hover{opacity:.85}
#surprise-btn{background:var(--surp-bg);color:var(--surp-text)}
#surprise-btn:hover{opacity:.85}
#show-btn{background:var(--show-bg);color:var(--show-text);border:1px solid var(--show-bdr);padding:5px 13px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px;text-transform:uppercase;flex-shrink:0}
#show-btn:hover{opacity:.85}
#show-btn.filtered{background:var(--show-f-bg);color:var(--show-f-text);border-color:var(--show-f-bdr)}
#setlist-btn{background:var(--sl-bg);color:var(--sl-text);border:1px solid var(--sl-bdr);padding:5px 13px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px;text-transform:uppercase;flex-shrink:0}
#setlist-btn:hover{opacity:.85}
#setlist-btn.has-tracks{background:var(--sl-on-bg);color:var(--sl-on-text);border-color:var(--sl-on-bdr)}
/* ── Modals ── */
#show-modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;align-items:flex-start;justify-content:center;padding-top:60px}
#show-modal-overlay.open{display:flex}
#show-modal{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:20px 24px;width:520px;max-width:90vw;max-height:80vh;overflow-y:auto;font-size:12px}
#show-modal h2{color:var(--col3);font-family:var(--lbl-font);font-size:13px;letter-spacing:2px;text-transform:uppercase;margin:0 0 16px;font-weight:600}
.show-profiles{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.profile-btn{background:var(--bg4);color:var(--text2);border:1px solid var(--border2);padding:5px 14px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;letter-spacing:.5px;transition:all .1s;text-transform:uppercase}
.profile-btn:hover{border-color:var(--col3);color:var(--col3)}
.profile-btn.active{color:var(--col3);border-color:var(--col3)}
.genre-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:16px}
.genre-chk{display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--text2);font-size:11px;padding:4px 6px;border-radius:3px}
.genre-chk:hover{background:var(--bg4);color:var(--text)}
.genre-chk input{accent-color:var(--col3);cursor:pointer}
.genre-chk.checked{color:var(--text)}
#show-apply{background:var(--col3);color:#000;border:none;padding:7px 20px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:12px;cursor:pointer;font-weight:bold;letter-spacing:1px;width:100%;text-transform:uppercase}
#show-apply:hover{opacity:.85}
#setlist-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;align-items:flex-start;justify-content:center;padding-top:60px}
#setlist-overlay.open{display:flex}
#setlist-modal{background:var(--bg3);border:1px solid var(--border2);border-radius:6px;padding:20px 24px;width:560px;max-width:90vw;max-height:80vh;overflow-y:auto;font-size:12px}
#setlist-modal h2{color:var(--sl-on-text);font-family:var(--lbl-font);font-size:13px;letter-spacing:2px;text-transform:uppercase;margin:0 0 4px;font-weight:600}
#setlist-subtitle{color:var(--text3);font-size:11px;margin-bottom:16px}
#setlist-list{list-style:none;padding:0;margin:0 0 14px}
#setlist-list li{display:flex;align-items:baseline;gap:10px;padding:5px 0;border-bottom:1px solid var(--border)}
#setlist-list .sl-num{color:var(--text4);min-width:22px;text-align:right;font-size:10px}
#setlist-list .sl-time{color:var(--text3);font-size:10px;min-width:38px}
#setlist-list .sl-artist{color:var(--text);font-weight:600}
#setlist-list .sl-title{color:var(--text2)}
#setlist-list .sl-genre{color:var(--text3);font-size:10px;margin-left:auto}
#setlist-empty{color:var(--text3);font-style:italic;padding:20px 0;text-align:center}
.setlist-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
#setlist-export-btn{flex:1;background:var(--sl-on-bg);color:var(--sl-on-text);border:1px solid var(--sl-on-bdr);padding:7px 14px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px;text-transform:uppercase}
#setlist-export-btn:hover{opacity:.85}
#setlist-newwin-btn{background:var(--bg4);color:var(--text2);border:1px solid var(--border2);padding:7px 14px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;letter-spacing:.5px;text-transform:uppercase}
#setlist-newwin-btn:hover{color:var(--text);border-color:var(--text2)}
#setlist-reset-btn{background:var(--rst-bg);color:var(--rst-text);border:1px solid var(--rst-bdr);padding:7px 14px;border-radius:var(--btn-r);font-family:var(--lbl-font);font-size:11px;cursor:pointer;letter-spacing:1px;text-transform:uppercase}
#setlist-reset-btn:hover{opacity:.85}
/* ── Structure ── */
#rescue-box{display:none!important}
#rescue-box .r-label{font-size:9px;letter-spacing:2px;color:var(--text3);margin-bottom:5px;text-transform:uppercase}
#rescue-box .r-track{font-size:13px;cursor:pointer}
#rescue-box .r-track .ra{color:var(--text2)}#rescue-box .r-track .rt{color:var(--text);font-weight:bold}
#search-wrap{background:var(--srch-bg);border-bottom:1px solid var(--border);padding:8px 18px;flex-shrink:0;position:relative}
#q{width:100%;background:var(--inp-bg);color:var(--text);border:1px solid var(--inp-bdr);padding:8px 13px;font-size:14px;font-family:inherit;border-radius:3px}
#q:focus{outline:none;border-color:var(--accent)}
#results{position:absolute;left:18px;right:18px;background:var(--res-bg);border:1px solid var(--inp-bdr);border-top:none;z-index:100;max-height:220px;overflow-y:auto;display:none}
.r{padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:10px}
.r:hover{background:var(--bg4)}
.r .ra{color:var(--text2)}.r .rt{color:var(--text);font-weight:bold}
#cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:var(--border2);flex:1;overflow:hidden}
.col{background:var(--bg);display:flex;flex-direction:column;overflow:hidden}
.col-hdr{padding:9px 14px;font-size:10px;letter-spacing:3px;text-transform:uppercase;border-bottom:1px solid var(--border);flex-shrink:0;font-family:var(--lbl-font)}
#c1 .col-hdr{color:var(--col1)}#c2 .col-hdr{color:var(--col2)}#c3 .col-hdr{color:var(--col3)}
.col-body{overflow-y:auto;flex:1;padding:10px}
.anchor-box{position:relative;background:var(--anchor-bg);border:1px solid var(--anchor-bdr);border-radius:4px;padding:12px}
.anchor-box .anc-art{float:right;width:56px;height:56px;object-fit:cover;border-radius:4px;margin:0 0 8px 12px;opacity:0.9}
.anchor-box .deck-tag{font-size:9px;color:var(--anchor-col);letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;opacity:0.7}
.anchor-box .an{font-size:14px;margin-bottom:5px}
.anchor-box .an .aa{color:var(--anchor-col)}.anchor-box .an .at{color:var(--text)}
.tk{position:relative;padding:9px 10px;margin-bottom:5px;border-radius:3px;cursor:pointer;border:1px solid var(--border)}
.tk:hover{border-color:var(--border2);background:var(--bg4)}
.tk.sel{border-color:var(--col2);background:var(--card-sel)}
.tk .tn{margin-bottom:4px}.tk .ta{color:var(--text2)}.tk .tt{color:var(--text)}
.meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;margin-top:3px}
.bpm{color:var(--bpm)}.key{color:var(--key)}.gen{color:var(--gen)}.scr{color:var(--scr)}.sts{color:#ffd700;letter-spacing:-1px}
.rep-convicted{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#450a0a;color:#f87171;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.rep-accused{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#431407;color:#fb923c;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.rep-settled{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#052e16;color:#86efac;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.lyric-flag{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#2d1b4e;color:#c4b5fd;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.instr-badge{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#1a1b3a;color:#a5b4fc;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px;border:1px solid #4338ca44}
.tk.instrumental{border-left:3px solid #818cf8}
.anchor-box.instrumental{border-left:3px solid #818cf8}
body.passthrough .instr-badge{background:#000;color:#a5b4fc;border-color:#4338ca}
body.passthrough .tk.instrumental{border-left-color:#818cf8;border-left-width:3px}
body.passthrough .anchor-box.instrumental{border-left:3px solid #818cf8}
body.day .instr-badge{background:#e6e4f0;color:#5b4cc9;border-color:#5b4cc9}
body.lcars .instr-badge{background:#9999CC;color:#000;border:none;border-radius:20px;font-family:'Oswald',sans-serif}
body.borg .instr-badge{background:#000;color:#00FFFF;border:1px dashed #00AAAA}
.lyric-summary{font-size:11px;color:var(--lyric);font-style:italic;margin-top:3px;white-space:normal;overflow-wrap:break-word;cursor:default}
#lyr-tooltip{display:none;position:fixed;z-index:9999;pointer-events:none}
#lyr-tooltip .tk{zoom:2;min-width:220px;max-width:260px;cursor:default!important;border-color:#444!important;background:#181818!important;margin-bottom:0!important;box-shadow:0 8px 32px rgba(0,0,0,.8)}
body.day #lyr-tooltip .tk{background:#e8e3dd!important;border-color:#aaa!important}
body.lcars #lyr-tooltip .tk{background:#0a0800!important;border-color:#FF990033!important}
body.borg #lyr-tooltip .tk{background:#000805!important;border-color:#00FF0022!important}
.tx{font-size:10px;padding:2px 6px;border-radius:3px;font-weight:bold;letter-spacing:1px;text-transform:uppercase}
.tx-beat{background:var(--tx-beat);color:var(--tx-beat-t)}.tx-frag{background:var(--tx-frag);color:var(--tx-frag-t)}
.tx-beatfx{background:var(--tx-fx);color:var(--tx-fx-t)}.tx-blend{background:var(--tx-blend);color:var(--tx-blend-t)}
.tx-stem{background:var(--tx-stem);color:var(--tx-stem-t)}.tx-loop{background:var(--tx-loop);color:var(--tx-loop-t)}
.tx-efx{background:var(--tx-efx);color:var(--tx-efx-t)}.tx-cut{background:var(--tx-cut);color:var(--tx-cut-t)}
.bg{margin-bottom:12px}
.bg-dest{font-size:10px;color:var(--col3);letter-spacing:2px;text-transform:uppercase;margin-bottom:5px;padding-left:6px;border-left:2px solid var(--col3)}
.empty{color:var(--text3);padding:16px;font-size:12px;text-align:center;line-height:1.8}
.deck-cards{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px;width:100%;box-sizing:border-box}
#anc-lyric{margin-top:8px}
.kl{padding:1px 0;line-height:1.5;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;text-align:center}
.kl-prev3{font-size:10px;color:var(--text);opacity:0.20}
.kl-prev2{font-size:11px;color:var(--text);opacity:0.35}
.kl-prev {font-size:12px;color:var(--text);opacity:0.65}
.kl-curr {font-size:14px;color:var(--text);font-weight:600;letter-spacing:0.01em}
.kl-next {font-size:12px;color:var(--text);opacity:0.65}
.kl-next2{font-size:11px;color:var(--text);opacity:0.35}
.kl-next3{font-size:10px;color:var(--text);opacity:0.20}
/* kl-word-hi / kl-word-lead removed — word sweep retired */
.kl-status{font-size:9px;color:var(--text3);text-align:right;padding:0 2px 2px;opacity:0.5;letter-spacing:0.05em}
.dc{padding:8px 10px;border-radius:4px;cursor:pointer;border:1px solid var(--border);background:var(--card-bg);transition:border-color .15s;min-width:0;overflow:hidden}
.dc:hover{border-color:var(--border2);background:var(--bg4)}
.dc.dc-idle{border-color:var(--border);color:var(--text3)}
.dc.dc-loaded{border-color:var(--border2);background:var(--bg4)}
.dc.dc-playing{border-color:var(--dc-play-bdr);background:var(--dc-play-bg);box-shadow:0 0 8px var(--dc-play-glow)}
.dc .dc-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;color:var(--text3);font-family:var(--lbl-font)}
.dc.dc-loaded .dc-label{color:var(--text2)}
.dc.dc-playing .dc-label{color:var(--accent)}
.dc .dc-name{font-size:12px;line-height:1.4;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.dc .dc-artist{color:var(--text2)}.dc .dc-title{color:var(--text)}.dc .dc-sep{color:var(--text3)}
.dc .dc-meta{font-size:10px;color:var(--meta);margin-top:3px}
.dc.dc-loaded .dc-meta{color:var(--text2)}
.dc .dc-empty{color:var(--text3);font-size:11px;font-style:italic}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--bg4);color:var(--text2);border:1px solid var(--border2);padding:6px 16px;border-radius:4px;font-size:11px;letter-spacing:1px;opacity:0;transition:opacity .15s;pointer-events:none;z-index:999}
#toast.show{opacity:1}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:var(--bg3)}::-webkit-scrollbar-thumb{background:var(--border2)}
#activity-bar{background:var(--bg3);border-bottom:1px solid var(--border);padding:5px 18px;display:none;align-items:center;gap:12px;flex-shrink:0;font-size:11px}
#activity-bar.active{display:flex}
#activity-bar .act-label{color:var(--act-col);letter-spacing:1px;text-transform:uppercase;font-size:10px;white-space:nowrap;min-width:80px}
#activity-bar .act-track{flex:1;color:var(--text2);white-space:normal;overflow-wrap:break-word;font-style:italic;font-size:10px}
#activity-bar .act-bar-wrap{width:160px;background:var(--border);border-radius:2px;height:4px;overflow:hidden;flex-shrink:0}
#activity-bar .act-fill{background:var(--act-col);height:100%;border-radius:2px;transition:width .4s}
#activity-bar .act-info{color:var(--text3);white-space:nowrap;font-size:10px}
/* ── Album art ── */
.art-thumb{position:absolute;top:6px;right:6px;width:36px;height:36px;border-radius:3px;object-fit:cover;opacity:0.82;flex-shrink:0}
.tip-art{width:56px;height:56px;border-radius:4px;object-fit:cover;float:right;margin:0 0 6px 10px;flex-shrink:0}
/* ══ LCARS structural overrides ══════════════════════════════════════════════ */
body.lcars #hdr{background:var(--col1);border-bottom:none;padding:0;min-height:40px}
body.lcars #hdr h1{font-family:'Oswald',sans-serif;font-weight:700;letter-spacing:6px;padding:0 20px;color:#000}
body.lcars #hdr small{font-family:'Oswald',sans-serif;padding-right:12px}
body.lcars #theme-btn,body.lcars #art-reload-btn,body.lcars #stt-btn{font-family:'Oswald',sans-serif;font-weight:700;background:#CC7700;color:#000;border:none;padding:0 16px;align-self:stretch;border-radius:0}
body.lcars #theme-btn:hover,body.lcars #art-reload-btn:hover,body.lcars #stt-btn:hover{background:#FFAA00}
body.lcars #osc-status{font-family:'Oswald',sans-serif;border-radius:0;padding:0 14px;align-self:stretch;display:flex;align-items:center}
body.lcars #osc-status.on{background:#004400;color:#00FF66}
body.lcars #osc-status.off{background:#330000;color:#FF3300}
body.lcars #deck-bar{border-bottom-width:3px}
body.lcars .deck-pill{border-radius:20px;border-width:2px;font-family:'Oswald',sans-serif;padding:3px 16px}
body.lcars .deck-pill.playing{color:#000;background:var(--accent)}
body.lcars #deck-msg{font-family:'Oswald',sans-serif}
body.lcars #swap-btn,body.lcars .panic-btn,body.lcars #show-btn,body.lcars #setlist-btn{border-radius:20px;font-family:'Oswald',sans-serif;font-weight:600;letter-spacing:2px}
body.lcars #swap-btn{border:2px solid #555}
body.lcars #show-btn,body.lcars #setlist-btn{border:none}
body.lcars .col-hdr{font-family:'Oswald',sans-serif;letter-spacing:4px;font-weight:700;padding:10px 18px;border-bottom:none}
body.lcars #c1 .col-hdr{background:var(--col1);color:#000}
body.lcars #c2 .col-hdr{background:var(--col2);color:#000}
body.lcars #c3 .col-hdr{background:var(--col3);color:#000}
body.lcars .anchor-box{border-width:2px}
body.lcars .profile-btn{border-radius:20px;font-family:'Oswald',sans-serif}
body.lcars #show-apply,body.lcars #setlist-export-btn,body.lcars #setlist-reset-btn{border-radius:20px;font-family:'Oswald',sans-serif;border:none}
body.lcars #setlist-newwin-btn{border-radius:20px;font-family:'Oswald',sans-serif}
/* ══ Borg structural overrides ═══════════════════════════════════════════════ */
body.borg,body.borg #q,body.borg #hdr h1{font-family:'Courier New',Courier,monospace}
body.borg #hdr{border-bottom-width:1px}
body.borg #osc-status.on{background:transparent;color:#00FF00;border:1px solid #004400}
body.borg #osc-status.off{background:transparent;color:#003300;border:1px solid #002200}
body.borg .deck-pill.playing{color:var(--accent);background:transparent}
body.borg .anchor-box{border-style:dashed}
body.borg .tk:hover{box-shadow:0 0 6px #00FF0011}
body.borg .dc.dc-playing{box-shadow:0 0 8px var(--dc-play-glow)}
/* ══ Passthrough structural overrides (Viture AR HUD mode) ════════════════════ */
.slot-num{display:none;color:#00FF00;font-family:'Courier New',monospace;font-weight:bold;font-size:14px;margin-right:8px;min-width:22px}
body.passthrough{background:#000}
body.passthrough #hdr{padding:3px 10px;border-bottom:0;min-height:22px;gap:8px}
body.passthrough #hdr h1{font-size:11px;letter-spacing:1.5px;opacity:0.5}
body.passthrough #tc,
body.passthrough #osc-status,
body.passthrough #pill-a,
body.passthrough #pill-b,
body.passthrough #deck-msg,
body.passthrough #show-btn,
body.passthrough #lyr-tooltip,
body.passthrough #activity-bar{display:none !important}
body.passthrough #deck-bar{padding:4px 10px;min-height:26px;border-bottom:0}
body.passthrough #cols{grid-template-columns:0.75fr 1fr 1fr;gap:8px}
/* Column 1 — Selected Song + Deck A/B (compact for glasses) */
body.passthrough #c1 .col-body{padding:0}
body.passthrough #c1 .empty{display:none}  /* hide "Load a track in Traktor" placeholder */
body.passthrough .anchor-box{background:#000;border:1px solid #003300;padding:8px 10px;border-radius:3px}
body.passthrough .anchor-box .deck-tag{color:#00FF00;opacity:0.8;font-size:9px}
body.passthrough .anchor-box .an{font-size:13px;margin-bottom:3px}
body.passthrough .anchor-box .an .aa{color:#00FF00}
body.passthrough .anchor-box .an .at{color:#FFF}
body.passthrough .anchor-box .anc-art{width:40px;height:40px;margin:0 0 4px 8px}
body.passthrough .anchor-box .meta{font-size:11px}
body.passthrough .deck-cards{grid-template-columns:1fr;gap:4px;margin-top:6px}
body.passthrough .dc{background:#000;border:1px solid #003300;padding:6px 8px;border-radius:3px;position:relative}
body.passthrough .dc-loaded{border-color:#00AA00}
body.passthrough .dc-playing{border-color:#00FF00;background:#001500;box-shadow:0 0 6px #00FF0033}
body.passthrough .dc .dc-label{color:#00FF00;font-size:9px;letter-spacing:2px}
body.passthrough .dc .dc-name{font-size:12px;line-height:1.2}
body.passthrough .dc .dc-meta{font-size:10px}
.dc-playing-badge{display:inline-block;color:#00FF00;font-weight:bold;font-size:10px;letter-spacing:1px;margin-left:6px;padding:1px 5px;border:1px solid #00FF00;border-radius:2px;animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.55}}
body.passthrough .dc .dc-artist{color:#00FF00}
body.passthrough .dc .dc-title{color:#FFF}
body.passthrough .dc .dc-meta{color:#88FF88;font-size:11px;opacity:0.8}
body.passthrough .dc-empty{color:#004400;font-size:11px}
body.passthrough .col-hdr{font-size:11px;letter-spacing:2px;opacity:0.7;border-bottom:1px solid #003300;color:#00FF00}
/* Compressed candidate cards (columns 2 & 3) — tuned for Viture 1920×1200 */
body.passthrough .tk{background:#000;border:1px solid #003300;padding:5px 8px 5px 42px;font-size:12px;margin-bottom:3px}
body.passthrough .art-thumb{width:32px;height:32px;left:6px;right:auto;top:6px;opacity:0.8}
body.passthrough .tk .tn{padding-right:0 !important;font-size:13px;line-height:1.2;margin-bottom:2px}
body.passthrough .tk.sel{border-color:#00FF00;background:#001500;box-shadow:0 0 8px #00FF0033}
body.passthrough .tk:hover{border-color:#00AA00}
body.passthrough .meta{font-size:10px;gap:5px;margin-top:1px}
body.passthrough .lyric-summary{font-size:10px;color:#88FF88;line-height:1.2;margin-top:2px}
body.passthrough .col-body{padding:6px 8px;overflow:hidden}
body.passthrough .slot-num{font-size:12px;margin-right:6px;min-width:18px}
body.passthrough .bg-dest{font-size:9px;letter-spacing:1.5px;color:#66FF66;margin:6px 0 3px 0;padding-bottom:2px}
/* Dynamic fit — single-line titles; JS shrinks font until they fit without cutoff */
body.passthrough .tn,
body.passthrough .dc-name,
body.passthrough .anchor-box .an{white-space:nowrap}
body.passthrough .lyric-summary{white-space:nowrap}
/* Viewport-level constraints: no scrollbars anywhere */
html,body.passthrough{overflow:hidden;height:100vh}
body.passthrough #cols{overflow:hidden;min-height:0;flex:1}
body.passthrough .col-body{overflow:hidden}
body.passthrough #q{font-size:15px;padding:6px 10px;background:#000;border:1px solid #003300;color:#00FF00}
body.passthrough #q::placeholder{color:#005500}
body.passthrough #q:focus{border-color:#00FF00;box-shadow:0 0 6px #00FF0044}
body.passthrough .slot-num{display:inline-block}
body.passthrough #theme-btn,
body.passthrough #art-reload-btn,
body.passthrough #stt-btn{font-size:11px;padding:2px 6px;border-color:#003300;color:#00FF00}
body.passthrough #stt-btn.listening{color:#ff3344;border-color:#ff3344}
body.passthrough .panic-btn,
body.passthrough #swap-btn,
body.passthrough #setlist-btn{background:#000;color:#00FF00;border:1px solid #003300;padding:4px 10px;font-size:11px}
body.passthrough .panic-btn:hover,
body.passthrough #swap-btn:hover,
body.passthrough #setlist-btn:hover{background:#001500;border-color:#00FF00}
/* ── Passthrough layout for Viture 1920×1200 ─────────────────────────────── */
#deck-strip,#selected-strip{display:none}  /* strips retired — all in col 1 */
body.passthrough #deck-strip,
body.passthrough #selected-strip{display:none !important}
/* Hide SETLIST in passthrough — don't need it mid-show */
body.passthrough #setlist-btn{display:none !important}
/* Clamp UI to Viture Luma Ultra dimensions; center if display is larger */
body.passthrough{display:flex;flex-direction:column;height:100vh;max-width:1920px;max-height:1200px;margin:0 auto}
body.passthrough #hdr        {flex-shrink:0}
body.passthrough #cols       {flex:1;min-height:0;grid-template-columns:0.8fr 1fr 1fr;gap:8px;overflow:hidden}
body.passthrough #search-wrap{flex-shrink:0;padding:4px 10px;order:5}
body.passthrough #deck-bar   {flex-shrink:0;border-top:0;order:6}
/* Column 1 (Selected Song + big card + decks at bottom) */
body.passthrough #c1          {display:flex;flex-direction:column;min-height:0}
body.passthrough #c1 .col-hdr {flex-shrink:0}
body.passthrough #c1 .col-body{flex:1;display:flex;flex-direction:column;min-height:0;gap:8px}
/* Big detailed anchor card fills the vertical middle of col 1 */
body.passthrough .anchor-box{flex:1;display:flex;flex-direction:column;padding:10px 12px;min-height:0;overflow:hidden}
body.passthrough .anchor-box .deck-tag{font-size:9px;letter-spacing:2px;margin-bottom:6px;flex-shrink:0}
body.passthrough .anchor-box .anc-art{float:none;width:100%;height:auto;max-height:40%;object-fit:contain;margin:0 0 8px 0;align-self:center}
body.passthrough .anchor-box .an{font-size:15px;margin-bottom:6px;flex-shrink:0}
body.passthrough .anchor-box .meta{font-size:12px;flex-wrap:wrap;margin-top:4px;flex-shrink:0}
body.passthrough .anchor-box .lyric-summary{color:#88FF88;margin-top:6px;line-height:1.35;flex:1;min-height:0;overflow:hidden;white-space:normal;text-align:justify;text-justify:inter-word}
.size-justified{text-align:justify;text-justify:inter-word}
body.passthrough .anchor-box .rep-convicted,
body.passthrough .anchor-box .rep-accused,
body.passthrough .anchor-box .rep-settled,
body.passthrough .anchor-box .lyric-flag{display:inline-block;margin:3px 3px 0 0}
/* Decks: side-by-side at the bottom of col 1, above action buttons */
body.passthrough .deck-cards{grid-template-columns:1fr 1fr !important;gap:6px;margin-top:auto;flex-shrink:0}
body.passthrough .dc{padding:6px 8px}
body.passthrough .dc .dc-label{font-size:9px}
body.passthrough .dc .dc-name{font-size:11px}
body.passthrough .dc .dc-meta{font-size:10px}
/* ── Add-track drop zone (hidden in passthrough / phone view) ──────────── */
body.passthrough #add-track-zone{display:none !important}
#add-track-zone{
  border:2px dashed var(--border,#2a2a2a);
  border-radius:6px;
  padding:8px 16px;
  margin:4px 0 6px 0;
  text-align:center;
  cursor:pointer;
  transition:background 0.15s,border-color 0.15s;
  font-size:12px;
  color:#555;
  user-select:none;
}
#add-track-zone:hover,#add-track-zone.drag-over{
  border-color:#4ade80;
  color:#aaa;
  background:rgba(74,222,128,0.04);
}
#add-track-icon{margin-right:6px}
#add-track-status{
  margin-top:4px;
  font-size:11px;
  color:#6ee7b7;
  min-height:14px;
}
#add-track-status.error{color:#f87171}
</style>
</head>
<body>
<div id="toast"></div>
<div id="lyr-tooltip"></div>
<div id="hdr">
  <h1>♪ Mac Bilby</h1>
  <small id="tc">loading…</small>
  <span id="osc-status" class="off">OSC OFF</span>
  <button id="theme-btn" onclick="toggleTheme()" title="Cycle themes: Night → Day → LCARS → Borg → Passthrough">🌙</button>
  <button id="art-reload-btn" onclick="reloadArt()" title="Reload album art index (after Syncthing sync)">🖼</button>
  <button id="stt-btn" onclick="toggleSTT()" title="Toggle voice recognition (Web Speech API). Continuous mode — speech goes into search box.">🎤</button>
</div>
<!-- Passthrough-only strips (hidden in all other themes via CSS) -->
<div id="deck-strip"></div>
<div id="selected-strip"></div>
<div id="deck-bar">
  <span class="deck-pill" id="pill-a">DECK A</span>
  <span class="deck-pill" id="pill-b">DECK B</span>
  <button id="swap-btn" onclick="swapDecks()" title="Swap Deck A ↔ B assignments">⇄ Swap</button>
  <span id="deck-msg">Waiting for Traktor… or search below</span>
  <button class="panic-btn" id="save-btn" onclick="rescueMe('save')" title="Best rated floor track near current BPM/genre">🚨 Save Me</button>
  <button class="panic-btn" id="surprise-btn" onclick="rescueMe('surprise')" title="Highly rated track you haven't played tonight">✨ Surprise Me</button>
  <button id="show-btn" onclick="openShowConfig()" title="Configure show genre filter">🎛 Show Setup</button>
  <button id="setlist-btn" onclick="openSetlist()" title="View played tracks and export setlist">📋 Setlist</button>
</div>
<!-- Show Config Modal -->
<div id="show-modal-overlay" onclick="closeShowConfig(event)">
  <div id="show-modal">
    <h2>🎛 Show Genre Setup</h2>
    <div class="show-profiles" id="show-profiles"></div>
    <div class="genre-grid" id="genre-grid"></div>
    <button id="show-apply" onclick="applyShowConfig()">Apply</button>
  </div>
</div>
<!-- Setlist Modal -->
<div id="setlist-overlay" onclick="closeSetlist(event)">
  <div id="setlist-modal">
    <h2>📋 Tonight's Setlist</h2>
    <div id="setlist-subtitle">Tracks confirmed played this show</div>
    <ul id="setlist-list"></ul>
    <div id="setlist-empty" style="display:none">No tracks played yet — setlist populates automatically as you play.</div>
    <div class="setlist-actions">
      <button id="setlist-export-btn" onclick="exportSetlist()">⬇ Copy for Social Media</button>
      <button id="setlist-newwin-btn" onclick="window.open('/setlist','_blank')">↗ Open in New Tab</button>
      <button id="setlist-reset-btn" onclick="resetShow()">↺ Reset Show</button>
    </div>
  </div>
</div>
<div id="activity-bar">
  <span class="act-label" id="act-label">PROCESSING</span>
  <span class="act-track" id="act-track"></span>
  <div class="act-bar-wrap"><div class="act-fill" id="act-fill" style="width:0%"></div></div>
  <span class="act-info" id="act-info"></span>
</div>
<div id="rescue-box">
  <div class="r-label" id="rescue-label">SAVE ME</div>
  <div class="r-track" id="rescue-track" onclick="rescueCopy()"></div>
  <div class="meta" id="rescue-meta" style="margin-top:6px"></div>
</div>
<div id="search-wrap">
  <input id="q" type="text" placeholder="Manual search — artist or title…" autocomplete="off" spellcheck="false">
  <div id="results"></div>
</div>
<div id="add-track-zone">
  <div id="add-track-inner">
    <span id="add-track-icon">🎵</span>
    <span id="add-track-label">Drop a track — Bilby will handle the whole thing</span>
    <input type="file" id="add-track-file" accept="audio/*" style="display:none">
  </div>
  <div id="add-track-status"></div>
</div>
<div id="cols">
  <div class="col" id="c1">
    <div class="col-hdr">① Selected Song</div>
    <div class="col-body" id="b1">
      <div class="empty">Load a track in Traktor<br>— or search above.</div>
    </div>
  </div>
  <div class="col" id="c2">
    <div class="col-hdr">② Play Next — Lock</div>
    <div class="col-body" id="b2"><div class="empty">Waiting…</div></div>
  </div>
  <div class="col" id="c3">
    <div class="col-hdr">③ After That — Bridge</div>
    <div class="col-body" id="b3"><div class="empty">Waiting…</div></div>
  </div>
</div>
<script>
let SR=[],S2=[],anchor=null,slot2=null,oscActive=false;
// Per-deck resolved track objects (or null if empty / not found in collection)
let deckTracks={a:null,b:null};
let deckPlaying={a:false,b:false};

// ── Theme cycle (Night → Day → LCARS → Borg) ─────────────────────────────────
const THEMES=['night','day','lcars','borg','passthrough'];
const THEME_ICONS={night:'🌙',day:'☀',lcars:'🖖',borg:'👾',passthrough:'🕶'};
(function(){
  // Migrate old 2-theme values ('light'/'dark') to new 4-theme names
  let saved=localStorage.getItem('theme')||'night';
  if(saved==='light')saved='day';
  if(saved==='dark' )saved='night';
  if(!THEMES.includes(saved))saved='night';
  localStorage.setItem('theme',saved);
  if(saved!=='night')document.body.classList.add(saved);
  document.getElementById('theme-btn').textContent=THEME_ICONS[saved];
})();
function toggleTheme(){
  const cur=THEMES.find(t=>document.body.classList.contains(t))||'night';
  const nxt=THEMES[(THEMES.indexOf(cur)+1)%THEMES.length];
  THEMES.forEach(t=>document.body.classList.remove(t));
  if(nxt!=='night')document.body.classList.add(nxt);
  document.getElementById('theme-btn').textContent=THEME_ICONS[nxt];
  localStorage.setItem('theme',nxt);
  // Clear any inline font-size the fit routine set (non-passthrough doesn't need it)
  document.querySelectorAll('[style*="font-size"]').forEach(el=>{el.style.fontSize=''});
  // Best-effort: resize window to match Viture Luma Ultra native resolution
  // (only works if this window was opened programmatically; silent no-op otherwise)
  if(nxt==='passthrough'){
    try{window.resizeTo(1920,1200);}catch(e){}
    try{window.moveTo(0,0);}catch(e){}
  }
  if(typeof fitPassthrough==='function')fitPassthrough();
}
function reloadArt(){
  const btn=document.getElementById('art-reload-btn');
  btn.textContent='⏳';
  fetch('/api/reload-art',{method:'POST'})
    .then(r=>r.json())
    .then(d=>{btn.textContent='🖼';console.log('Art index reloaded:',d.count,'entries');})
    .catch(()=>{btn.textContent='🖼';});
}

// ── Auto-focus: whenever this window gains focus, cursor → #q ─────────────
function focusSearch(){
  const q=document.getElementById('q');
  if(!q)return;
  // Don't steal focus from modals or other inputs
  const modalOpen=document.getElementById('show-modal-overlay')?.classList.contains('show')
                ||document.getElementById('setlist-overlay')?.classList.contains('show');
  if(modalOpen)return;
  const a=document.activeElement;
  if(a&&a!==document.body&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA')&&a!==q)return;
  q.focus();
}
window.addEventListener('focus',focusSearch);
window.addEventListener('load',()=>setTimeout(focusSearch,100));
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')setTimeout(focusSearch,50);});
// Re-focus after any click that isn't on a button/card/input
document.addEventListener('click',e=>{
  if(e.target.closest('input,textarea,button,a,.tk,.dc,.anchor-box,.r'))return;
  setTimeout(focusSearch,10);
});

// ── Speech recognition (Web Speech API) ───────────────────────────────────
// Voice flows straight into #q — keyword commands fire on final transcripts,
// free-text falls through into the live-search pipeline.
let _stt=null, _sttOn=false;
function sttSupported(){return !!(window.SpeechRecognition||window.webkitSpeechRecognition);}
function sttBtn(){return document.getElementById('stt-btn');}
function sttPaint(){
  const b=sttBtn();if(!b)return;
  b.textContent=_sttOn?'🔴':'🎤';
  b.classList.toggle('listening',_sttOn);
}
function startSTT(){
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){toast('Voice recognition not supported in this browser');return;}
  if(_stt){try{_stt.stop();}catch(e){}}
  _stt=new SR();
  _stt.continuous=true;
  _stt.interimResults=true;
  _stt.lang='en-US';
  _stt.onresult=ev=>{
    let interim='', final='';
    for(let i=ev.resultIndex;i<ev.results.length;i++){
      const r=ev.results[i];
      if(r.isFinal)final+=r[0].transcript;else interim+=r[0].transcript;
    }
    const q=document.getElementById('q');
    if(!q)return;
    if(interim&&!final){q.value=interim.trim();}
    if(final){
      const txt=final.trim();
      q.value=txt;
      if(tryKeywordCommand(txt)){
        q.value='';
        document.getElementById('results').style.display='none';
        toast(`🎤 ${txt}`);
      }else{
        // Not a command — kick the live search pipeline
        q.dispatchEvent(new Event('input',{bubbles:true}));
      }
    }
  };
  _stt.onerror=ev=>{
    console.warn('STT error:',ev.error);
    if(ev.error==='not-allowed'||ev.error==='service-not-allowed'){
      toast('🎤 Microphone permission denied');
      _sttOn=false;sttPaint();
    }
  };
  _stt.onend=()=>{
    // Chrome auto-stops after silence — re-arm if still on
    if(_sttOn){try{_stt.start();}catch(e){}}
    else sttPaint();
  };
  try{_stt.start();_sttOn=true;sttPaint();toast('🎤 Listening…');}
  catch(e){toast('🎤 Failed to start: '+e.message);}
}
function toggleSTT(){
  if(!sttSupported()){toast('Voice recognition not supported');return;}
  if(_sttOn){_sttOn=false;try{_stt.stop();}catch(e){}sttPaint();toast('🎤 Stopped');}
  else{startSTT();}
}

// ── Keyword commands — typed (or voice-dictated) into #q, fire on Enter ─────
const KEYWORD_CMDS=[
  {re:/^swap\s+decks?$/i,          fn:()=>swapDecks()},
  {re:/^save\s+me$/i,              fn:()=>rescueMe('save')},
  {re:/^surprise\s+me$/i,          fn:()=>rescueMe('surprise')},
  {re:/^open\s+setlist$/i,         fn:()=>openSetlist()},
  {re:/^open\s+show$/i,            fn:()=>openShowConfig()},
  {re:/^select\s+(\d{1,2})$/i,     fn:m=>selectSlot2Num(parseInt(m[1])-1)},
  {re:/^select\s+([ab])$/i,        fn:m=>loadSelectedToDeck(m[1].toLowerCase())},
];
function tryKeywordCommand(txt){
  for(const {re,fn} of KEYWORD_CMDS){
    const m=txt.match(re);
    if(m){fn(m);return true;}
  }
  return false;
}
function selectSlot2Num(idx){
  if(!S2||idx<0||idx>=S2.length){toast(`No candidate ${idx+1}`);return;}
  pickSlot2(idx);
  const t=S2[idx];
  toast(`Selected [${idx+1}] ${t.artist} — ${t.title}`);
}
async function loadSelectedToDeck(letter){
  if(!slot2){toast('Nothing selected — say "select 1" first');return;}
  try{
    const r=await fetch(`/api/load-to-deck?deck=${letter}&path=${encodeURIComponent(slot2.path)}`,{method:'POST'});
    if(r.ok){toast(`Loaded → Deck ${letter.toUpperCase()}`);}
    else{toast('Load failed');}
  }catch(e){toast('Load failed');}
}
// ── External text injection — DJ service, OCR, voice-to-text → /api/input-text
function injectInputText(text){
  text=(text||'').trim();
  if(!text)return;
  const qEl=document.getElementById('q');
  if(tryKeywordCommand(text)){
    qEl.value='';
    document.getElementById('results').style.display='none';
    toast(`⌨ ${text}`);
  }else{
    qEl.value=text;
    qEl.dispatchEvent(new Event('input',{bubbles:true}));
    qEl.focus();
  }
}

// ── Show Genre Config ─────────────────────────────────────────────────────────
const ALL_GENRES=[
  "Gothic Rock","Darkwave","Post-Punk","EBM","Industrial",
  "New Wave","Synthpop","Electronic","Ambient",
  "Shoegaze","Coldwave","Neofolk","Death Rock","Goth",
  "Witch House","Minimal Wave","Power Electronics","Noise",
  "Alternative Rock","Punk","Hard Rock","Metal","Indie Rock","Rock",
];
const SHOW_PROFILES={
  "Pure Goth":     ["Gothic Rock","Post-Punk","Darkwave","Death Rock","Goth","Shoegaze","Coldwave","Neofolk"],
  "Goth Industrial":["Gothic Rock","Darkwave","Post-Punk","EBM","Industrial","New Wave","Synthpop","Electronic","Ambient","Shoegaze","Coldwave","Neofolk","Death Rock","Goth","Witch House","Minimal Wave","Power Electronics","Noise"],
  "Dark Electronic":["EBM","Industrial","Electronic","Synthpop","Minimal Wave","Power Electronics","Witch House","Noise","Darkwave","Ambient"],
  "Open Floor":    null,
};
let _showCfgGenres=null; // mirrors server state; null=no filter

function buildShowModal(){
  // Profile buttons
  const pDiv=document.getElementById('show-profiles');
  pDiv.innerHTML='';
  for(const [name,genres] of Object.entries(SHOW_PROFILES)){
    const b=document.createElement('button');
    b.className='profile-btn';b.textContent=name;
    b.onclick=()=>{
      if(genres===null){
        document.querySelectorAll('.genre-chk input').forEach(c=>c.checked=false);
      } else {
        document.querySelectorAll('.genre-chk input').forEach(c=>{
          c.checked=genres.includes(c.value);
          c.closest('.genre-chk').classList.toggle('checked',c.checked);
        });
      }
      document.querySelectorAll('.profile-btn').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
    };
    pDiv.appendChild(b);
  }
  // Genre checkboxes
  const gDiv=document.getElementById('genre-grid');
  gDiv.innerHTML='';
  ALL_GENRES.forEach(g=>{
    const checked=_showCfgGenres===null?false:_showCfgGenres.includes(g);
    const lbl=document.createElement('label');
    lbl.className='genre-chk'+(checked?' checked':'');
    lbl.innerHTML=`<input type="checkbox" value="${g}"${checked?' checked':''}> ${g}`;
    lbl.querySelector('input').onchange=e=>{
      lbl.classList.toggle('checked',e.target.checked);
      document.querySelectorAll('.profile-btn').forEach(x=>x.classList.remove('active'));
    };
    gDiv.appendChild(lbl);
  });
  // Highlight active profile
  syncProfileHighlight();
}
function syncProfileHighlight(){
  const checked=[...document.querySelectorAll('.genre-chk input:checked')].map(c=>c.value).sort().join(',');
  document.querySelectorAll('.profile-btn').forEach(b=>{
    const pGenres=SHOW_PROFILES[b.textContent];
    const pKey=pGenres===null?'':([...pGenres]).sort().join(',');
    b.classList.toggle('active', pGenres===null?checked==='':checked===pKey);
  });
}
async function openShowConfig(){
  // Fetch current server state
  const r=await fetch('/api/show-config');
  const d=await r.json();
  _showCfgGenres=d.genres;
  buildShowModal();
  document.getElementById('show-modal-overlay').classList.add('open');
}
function closeShowConfig(e){
  if(e&&e.target!==document.getElementById('show-modal-overlay'))return;
  document.getElementById('show-modal-overlay').classList.remove('open');
}
async function applyShowConfig(){
  const checked=[...document.querySelectorAll('.genre-chk input:checked')].map(c=>c.value);
  // If none checked, treat as open floor
  const payload={genres: checked.length?checked:null};
  await fetch('/api/show-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  _showCfgGenres=payload.genres;
  const btn=document.getElementById('show-btn');
  if(payload.genres===null){
    btn.textContent='🎛 Show Setup';btn.classList.remove('filtered');
  } else {
    btn.textContent=`🎛 ${payload.genres.length} genres`;btn.classList.add('filtered');
  }
  document.getElementById('show-modal-overlay').classList.remove('open');
  toast('Show config applied');
}
// Init: fetch current config on load to reflect server state
(async()=>{
  try{
    const r=await fetch('/api/show-config');
    const d=await r.json();
    _showCfgGenres=d.genres;
    const btn=document.getElementById('show-btn');
    if(d.genres&&d.genres.length){btn.textContent=`🎛 ${d.genres.length} genres`;btn.classList.add('filtered');}
  }catch(e){}
})();
// ── Setlist ───────────────────────────────────────────────────────────────────
let _setlistData=[];
async function openSetlist(){
  await refreshSetlist();
  document.getElementById('setlist-overlay').classList.add('open');
}
function closeSetlist(e){
  if(e&&e.target!==document.getElementById('setlist-overlay'))return;
  document.getElementById('setlist-overlay').classList.remove('open');
}
async function refreshSetlist(){
  try{
    const r=await fetch('/api/setlist');
    const d=await r.json();
    _setlistData=d.setlist||[];
    renderSetlist(_setlistData);
    const btn=document.getElementById('setlist-btn');
    if(_setlistData.length){btn.classList.add('has-tracks');btn.textContent=`📋 Setlist (${_setlistData.length})`;}
    else{btn.classList.remove('has-tracks');btn.textContent='📋 Setlist';}
  }catch(e){}
}
function renderSetlist(sl){
  const ul=document.getElementById('setlist-list');
  const em=document.getElementById('setlist-empty');
  ul.innerHTML='';
  if(!sl||!sl.length){em.style.display='';return;}
  em.style.display='none';
  sl.forEach((e,i)=>{
    const li=document.createElement('li');
    li.innerHTML=`<span class="sl-num">${i+1}</span>`
      +`<span class="sl-time">${e.played_at||''}</span>`
      +`<span class="sl-artist">${e.artist}</span>`
      +`<span class="sl-title">— ${e.title}</span>`
      +`<span class="sl-genre">${e.genre||''}</span>`;
    ul.appendChild(li);
  });
}
async function exportSetlist(){
  const r=await fetch('/api/export-setlist');
  const text=await r.text();
  try{
    await navigator.clipboard.writeText(text);
    toast('Setlist copied to clipboard ✓');
  }catch(e){
    // Fallback: open in new tab
    const w=window.open('');
    w.document.write('<pre style="font-family:monospace;background:#000;color:#0f0;padding:20px">'+text.replace(/</g,'&lt;')+'</pre>');
    w.document.close();
  }
}
async function resetShow(){
  if(!confirm('Reset show? This clears the setlist and played-artist history.'))return;
  await fetch('/api/setlist',{method:'DELETE'});
  _setlistData=[];
  renderSetlist([]);
  document.getElementById('setlist-btn').classList.remove('has-tracks');
  document.getElementById('setlist-btn').textContent='📋 Setlist';
  toast('Show reset — fresh start!');
}
// Poll setlist count every 30s to keep button badge current
setInterval(refreshSetlist,30000);
refreshSetlist();

const q=document.getElementById('q'),
      res=document.getElementById('results'),
      b1=document.getElementById('b1'),
      b2=document.getElementById('b2'),
      b3=document.getElementById('b3'),
      oscEl=document.getElementById('osc-status'),
      deckMsg=document.getElementById('deck-msg'),
      pillA=document.getElementById('pill-a'),
      pillB=document.getElementById('pill-b');

fetch('/api/count').then(r=>r.json()).then(d=>{
  document.getElementById('tc').textContent=d.count+' tracks';
  if(d.osc) setOscOn();
});

function pollActivity(){
  fetch('/api/activity').then(r=>r.json()).then(d=>{
    const bar=document.getElementById('activity-bar');
    if(!d){bar.classList.remove('active');return;}
    bar.classList.add('active');
    document.getElementById('act-label').textContent=(d.task||'processing').toUpperCase();
    const pct=d.total?Math.round(d.done/d.total*100):0;
    document.getElementById('act-fill').style.width=pct+'%';
    document.getElementById('act-track').textContent=d.current||'';
    let info=`${d.done||0}/${d.total||'?'}`;
    if(d.eta_min!=null) info+=` — ${d.eta_min}m left`;
    document.getElementById('act-info').textContent=info;
  }).catch(()=>{});
}
setInterval(pollActivity,2500);
pollActivity();

function setOscOn(){
  oscActive=true;
  oscEl.textContent='OSC LIVE';oscEl.className='on';
}

function stars(n){
  return '<span class="sts">'+'★'.repeat(n)+'<span style="color:#2a2a2a">'+'·'.repeat(5-n)+'</span></span>';
}
const TX_CLASS={'BEAT MATCH':'beat','BEAT+FRAGMENT':'frag','BEAT+FX':'beatfx',
  'STEM BLEND':'stem','BLEND':'blend','LOOP DROP':'loop','EFFECT FADE':'efx','CUT':'cut'};
function txBadge(t){
  // Hidden pending rebuild as auto-transition mode — data still calculated, just not shown
  return'';
  // return`<span class="tx tx-${TX_CLASS[t.transition]||'cut'}">${t.transition}</span>`;
}
function repBadge(t){
  let out='';
  if(t.rep_tier){
    const cls=`rep-${t.rep_tier}`;
    const icon=t.rep_tier==='convicted'?'🔴 CONVICTED':t.rep_tier==='settled'?'🟢 SETTLED':'⚠ ACCUSED';
    out+=`<span class="${cls}" title="${esc(t.rep_summary||'')}">${icon}</span>`;
  }
  if(t.song_flag){
    out+=`<span class="rep-accused" title="${esc(t.song_flag)}" style="background:#1a1a00;color:#fde68a">⚠ THIS SONG</span>`;
  }
  return out;
}
function lyricBadges(t){
  if(!t.lyric_flags||!t.lyric_flags.length)return'';
  const labels={racism:'🚫 RACIST LYRICS',bigotry:'🚫 BIGOTED LYRICS',
    sexual_violence:'🚫 SEXUAL VIOLENCE',child_abuse:'🚫 CHILD ABUSE',
    extreme_violence:'🚫 EXTREME VIOLENCE'};
  return t.lyric_flags.map(f=>`<span class="lyric-flag" title="Lyric content warning">${labels[f]||'🚫 FLAGGED'}</span>`).join('');
}
function instrBadge(t){
  return t.is_instrumental?'<span class="instr-badge" title="Instrumental — no vocals">♬ INSTR</span>':'';
}
function lyricLine(t){
  // Plain summary line — used in suggestion cards. #anc-lyric wrapper is
  // added only in loadAnchor() so the ID is unique in the DOM.
  if(!t.lyric_summary)return'';
  return`<div class="lyric-summary">♪ ${esc(t.lyric_summary)}</div>`;
}
function tipCardHtml(t){
  const lyrHtml=t.lyric_summary
    ?`<div class="lyric-summary" style="white-space:normal;overflow-wrap:break-word">♪ ${esc(t.lyric_summary)}</div>`
    :'';
  const artHtml=t.art_url?`<img class="tip-art" src="${t.art_url}" onerror="this.style.display='none'" alt="">`:'';
  const cls='tk'+(t.is_instrumental?' instrumental':'');
  return`<div class="${cls}">${artHtml}<div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${instrBadge(t)}${lyricBadges(t)}</div>${lyrHtml}${meta(t,true)}</div>`;
}
function meta(t,showScore){
  return`<div class="meta">
    <span class="bpm">${t.bpm} BPM</span><span class="key">${t.key||'—'}</span>
    <span class="gen">${t.genre||'—'}</span>${stars(t.stars)}
    ${showScore?`<span class="scr">${t.score}%</span>`:''}${txBadge(t)}</div>`;
}
function tkHtml(t,idx,sel,showScore){
  const slotNum=`<span class="slot-num">[${idx+1}]</span>`;
  const artHtml=t.art_url?`<img class="art-thumb" src="${t.art_url}" loading="lazy" onerror="this.style.display='none'" alt="">`:'';
  const cls='tk'+(sel?' sel':'')+(t.is_instrumental?' instrumental':'');
  return`<div class="${cls}" id="s2-${idx}" data-track="${esc(JSON.stringify(t))}" onclick="pickSlot2(${idx});copyTrack('${esc(t.artist)}','${esc(t.title)}')">
    ${artHtml}<div class="tn" style="${t.art_url?'padding-right:42px':''}">${slotNum}<span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${instrBadge(t)}${lyricBadges(t)}</div>
    ${lyricLine(t)}${meta(t,showScore)}</div>`;
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

// ── Card tooltip — 2× enlarged version of any card with data-track ───────
const _tip=document.getElementById('lyr-tooltip');
let _tipTimer=null;
let _tipActive=null; // last element that triggered tooltip
function _showTip(el,e){
  if(_tipActive===el&&_tip.style.display==='block'){_positionTip(e);return;}
  clearTimeout(_tipTimer);
  let t;try{t=JSON.parse(el.dataset.track);}catch(ex){return;}
  _tip.innerHTML=tipCardHtml(t);
  _tip.style.display='block';
  _tipActive=el;
  _positionTip(e);
}
document.addEventListener('mouseover',e=>{
  const el=e.target.closest('[data-track]');
  if(!el){clearTimeout(_tipTimer);_tipTimer=setTimeout(()=>{_tip.style.display='none';_tipActive=null;},120);return;}
  _showTip(el,e);
});
document.addEventListener('mousemove',e=>{
  const el=e.target.closest('[data-track]');
  if(el) _showTip(el,e);
});
document.addEventListener('mouseout',e=>{
  const going=e.relatedTarget;
  if(going&&going.closest&&going.closest('[data-track]'))return;
  _tipTimer=setTimeout(()=>{_tip.style.display='none';_tipActive=null;},120);
});
function _positionTip(e){
  const pad=16, tw=_tip.offsetWidth, th=_tip.offsetHeight;
  let x=e.clientX+pad, y=e.clientY-th-pad;
  if(x+tw>window.innerWidth-pad) x=e.clientX-tw-pad;
  if(y<pad) y=e.clientY+pad+30;
  _tip.style.left=x+'px';
  _tip.style.top=y+'px';
}

let _toastTimer;
function copyTrack(artist,title){
  const text=`${artist} ${title}`;
  navigator.clipboard.writeText(text).catch(()=>{});
  const t=document.getElementById('toast');
  t.textContent='📋 '+text;
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer=setTimeout(()=>t.classList.remove('show'),1800);
}

// ── Deck cards ──────────────────────────────────────────────────────────────
function dcCardHtml(deck){
  const t=deckTracks[deck];
  const playing=deckPlaying[deck];
  const cls=t?(playing?'dc dc-playing':'dc dc-loaded'):'dc dc-idle';
  const playBadge=playing?'<span class="dc-playing-badge" title="Playing (lsof detected)">▶ PLAYING</span>':'';
  const label=`DECK ${deck.toUpperCase()} ${playBadge}`;
  const dcArt=t&&t.art_url?`<img src="${t.art_url}" style="float:right;width:32px;height:32px;object-fit:cover;border-radius:3px;margin:0 0 4px 8px;opacity:0.85" onerror="this.style.display='none'" alt="">`:''
  const body=t
    ? `${dcArt}<div class="dc-name"><span class="dc-artist">${esc(t.artist)}</span><span class="dc-sep"> — </span><span class="dc-title">${esc(t.title)}</span></div>
       <div class="dc-meta">${t.bpm} BPM · ${t.key||'—'} · ${t.genre||'—'}</div>`
    : `<div class="dc-empty">Nothing loaded</div>`;
  const click=t?`onclick="setDeckAnchor('${deck}')"`:'' ;
  const trackData=t?`data-track="${esc(JSON.stringify(t))}"`:'' ;
  return`<div class="${cls}" ${click} ${trackData}><div class="dc-label">${label}</div>${body}</div>`;
}

function renderDeckCards(){
  // Only show deck cards section if b1 has an anchor-box (otherwise just show them standalone)
  const existing=b1.querySelector('.deck-cards');
  const html=`<div class="deck-cards">${dcCardHtml('a')}${dcCardHtml('b')}</div>`;
  if(existing){
    existing.outerHTML=html;
  } else {
    // Append after anchor-box or as the only content if empty
    const ab=b1.querySelector('.anchor-box');
    if(ab) ab.insertAdjacentHTML('afterend',html);
    else b1.innerHTML=`<div class="empty">Load a track in Traktor<br>— or search above.</div>`+html;
  }
  if(typeof fitPassthrough==='function')fitPassthrough();
}

// ── Passthrough TOP strip: horizontal Deck A / Deck B readout (world zone) ──
function renderDeckStrip(){
  const el=document.getElementById('deck-strip');
  if(!el)return;
  el.innerHTML=['a','b'].map(d=>{
    const t=deckTracks[d];
    const playing=deckPlaying[d];
    const cls='ds-cell'+(!t?' ds-empty':playing?' ds-playing':'');
    const label=`DECK ${d.toUpperCase()}`;
    if(!t)return`<div class="${cls}"><span class="ds-label">${label}</span><span class="ds-title" style="color:#444">—</span></div>`;
    const art=t.art_url?`<img class="ds-art" src="${t.art_url}" onerror="this.style.display='none'" alt="">`:'';
    const play=playing?'<span class="ds-play">▶</span>':'';
    return`<div class="${cls}"><span class="ds-label">${label}</span>${play}${art}<span class="ds-artist">${esc(t.artist)}</span><span class="ds-sep">—</span><span class="ds-title">${esc(t.title)}</span><span class="ds-bpm">${t.bpm||'?'} BPM</span></div>`;
  }).join('');
}

// ── Passthrough BOTTOM strip: Selected Song readout (self zone) ──
function renderSelectedStrip(){
  const el=document.getElementById('selected-strip');
  if(!el)return;
  if(!anchor){
    el.className='ss-empty';
    el.innerHTML=`<div class="ss-label">① SELECTED SONG</div><div class="ss-row">Pick one from column ② — say "select 3" or click</div>`;
    return;
  }
  el.className='';
  const art=anchor.art_url?`<img class="ss-art" src="${anchor.art_url}" onerror="this.style.display='none'" alt="">`:'';
  const starsStr=typeof stars==='function'?stars(anchor.stars||0):'';
  el.innerHTML=`<div class="ss-label">① SELECTED SONG</div>
    <div class="ss-row">${art}<span class="ss-artist">${esc(anchor.artist)}</span><span class="ss-sep">—</span><span class="ss-title">${esc(anchor.title)}</span><span class="ss-meta">${anchor.bpm||'?'} BPM · ${esc(anchor.key||'—')} · ${esc(anchor.genre||'—')} ${starsStr}</span></div>`;
}

let _rescueTrack=null;
async function rescueMe(mode){
  const anchorParam=anchor?'&anchor='+encodeURIComponent(anchor.path):'';
  const url=mode==='save'?`/api/save-me?${anchorParam.slice(1)}`:`/api/surprise-me`;
  const t=await fetch(url).then(r=>r.json());
  if(t.error){alert('No candidates found.');return}
  _rescueTrack=t;
  // Clear search bar — Selected Song now populated by rescue
  q.value='';res.style.display='none';
  // Load as anchor — fires full suggestion pipeline just like a search pick or deck load
  await loadAnchor(t, null);
  // Flash the rescue label briefly so the Captain knows which button fired it
  const box=document.getElementById('rescue-box');
  document.getElementById('rescue-label').textContent=mode==='save'?'🚨 SAVE ME — loaded as anchor':'✨ SURPRISE ME — loaded as anchor';
  document.getElementById('rescue-track').innerHTML=
    `<span class="ra">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="rt">${esc(t.title)}</span>`;
  document.getElementById('rescue-meta').innerHTML=
    `<span class="bpm">${t.bpm} BPM</span><span class="key">${t.key||'—'}</span><span class="gen">${t.genre||'—'}</span>${stars(t.stars)}<span class="scr">${t.score}%</span>`;
  copyTrack(t.artist,t.title);
}
function rescueCopy(){
  if(_rescueTrack) copyTrack(_rescueTrack.artist,_rescueTrack.title);
}

async function swapDecks(){
  await fetch('/api/swap-decks',{method:'POST'});
  // Also swap local state
  [deckTracks.a, deckTracks.b] = [deckTracks.b, deckTracks.a];
  [deckPlaying.a, deckPlaying.b] = [deckPlaying.b, deckPlaying.a];
  renderDeckCards();
}

async function setDeckAnchor(deck){
  const t=deckTracks[deck];
  if(!t)return;
  await loadAnchor(t,deck);
}

// ── SSE — auto-detect from Traktor ─────────────────────────────────────────
let _sseSource=null;
function connectSSE(){
  if(_sseSource){try{_sseSource.close();}catch(e){} _sseSource=null;}
  const es=new EventSource('/api/events');
  _sseSource=es;
  es.onmessage=e=>{
    if(es!==_sseSource) return;  // stale — discard
    const d=JSON.parse(e.data);
    if(d.type==='connected'){
      setOscOn();
      // Restore deck state after reconnect
      restoreDeckStatus();
      return;
    }
    if(d.type==='play_state'){deckPlayState(d.deck,d.playing);return}
    if(d.type==='input_text'){injectInputText(d.text);return}
    if(d.type==='position'){
      if(_karLines&&_karDeck===d.deck){
        if(!_karLive){
          _karLive=true;
          // Real position events are flowing — kill the client-side fallback timer
          if(_karTimerId){clearInterval(_karTimerId);_karTimerId=null;}
          const st=document.getElementById('kl-status');
          if(st) st.textContent=_karStatusText(d.elapsed*1000);
        }
        renderKaraoke(_karLines,d.elapsed*1000);
      }
      return;
    }
    if(d.title||d.artist) deckLoaded(d.deck,d.title,d.artist,d.type==='playing');
  };
  es.onerror=()=>{
    if(es!==_sseSource) return;  // stale
    oscEl.textContent='SSE…';oscEl.className='off';
    setTimeout(connectSSE,3000);
  };
}
connectSSE();

async function restoreDeckStatus(){
  try{
    const s=await fetch('/api/deck-status').then(r=>r.json());
    if(s.a) await _resolveAndStoreDeck('a',s.a.title,s.a.artist);
    if(s.b) await _resolveAndStoreDeck('b',s.b.title,s.b.artist);
    let anchorDeck=null;
    if(s.playing_a){deckPlaying.a=true;pillA.className='deck-pill playing';anchorDeck='a';}
    else if(s.a){pillA.className='deck-pill loaded';}
    if(s.playing_b){deckPlaying.b=true;pillB.className='deck-pill playing';if(!anchorDeck)anchorDeck='b';}
    else if(s.b){pillB.className='deck-pill loaded';}
    if(!anchorDeck&&s.a) anchorDeck='a';
    renderDeckCards();
    // Fire full suggestion pipeline for whichever deck is active
    if(anchorDeck&&deckTracks[anchorDeck]) await loadAnchor(deckTracks[anchorDeck],anchorDeck);
  }catch(err){}
}

async function _resolveAndStoreDeck(deck,title,artist){
  const r=await fetch(`/api/resolve-deck?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}`).then(r=>r.json());
  deckTracks[deck]=r||{artist,title,bpm:'?',key:'',genre:'',stars:0,path:''};
}

function deckPlayState(deck,playing){
  deckPlaying[deck]=playing;
  const pill=deck==='a'?pillA:pillB;
  const other=deck==='a'?pillB:pillA;
  const otherDeck=deck==='a'?'b':'a';
  if(playing){
    pill.className='deck-pill playing';
    if(deckPlaying[otherDeck]){deckPlaying[otherDeck]=false;other.className='deck-pill loaded';}
  } else {
    if(pill.className.includes('playing')) pill.className=deckTracks[deck]?'deck-pill loaded':'deck-pill';
  }
  renderDeckCards();
  updateKaraokeSource();
}

async function deckLoaded(deck,title,artist,isPlaying=false){
  const pill=deck==='a'?pillA:pillB;
  if(!pill.className.includes('playing')) pill.className='deck-pill loaded';
  deckMsg.textContent=`Deck ${deck.toUpperCase()} ${isPlaying?'▶':'→'} ${artist} — ${title}`;

  const r=await fetch(`/api/resolve-deck?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}`).then(r=>r.json());
  deckTracks[deck]=r||{artist,title,bpm:'?',key:'',genre:'',stars:0,path:''};
  renderDeckCards();
  updateKaraokeSource();

  if(r){
    SR=[r];
    await loadAnchor(r,deck);
  } else {
    // Track not in collection — update anchor area but keep deck cards
    const ab=b1.querySelector('.anchor-box');
    const cardsEl=b1.querySelector('.deck-cards');
    const notFound=`<div class="empty" style="color:#777">Deck ${deck.toUpperCase()}: <b style="color:#bbb">${esc(artist)} — ${esc(title)}</b><br><span style="color:#555">Not in collection</span></div>`;
    if(ab) ab.outerHTML=notFound; else if(!cardsEl) b1.innerHTML=notFound;
    if(cardsEl) renderDeckCards();
    b2.innerHTML='<div class="empty">—</div>';
    b3.innerHTML='<div class="empty">—</div>';
  }
}

// ── Karaoke lyrics display ─────────────────────────────────────────────────
let _karLines=null;     // [{ms,text}] for playing track, or null
let _karDeck=null;      // 'a'|'b'|null — deck being tracked for position events
let _karAnchor=null;    // anchor track object currently driving karaoke
let _karMode='';        // 'lrc' | 'est' | ''
let _karLive=false;     // true once a real position event has arrived for this track
let _karWallStart=0;    // Date.now() when karaoke source was set — fallback timer base
let _karTimerId=null;   // setInterval id for client-side position fallback

function parseLRC(lrc){
  const lines=[];
  for(const l of lrc.split('\n')){
    const m=l.match(/^\[(\d+):(\d+\.\d+)\](.*)/);
    if(m){
      const ms=(parseInt(m[1])*60+parseFloat(m[2]))*1000;
      const text=m[3].trim();
      if(text) lines.push({ms,text});
    }
  }
  return lines;
}

function estimateLines(plainText,durationMs,vocalInMs,vocalOutMs){
  if(!plainText||!durationMs) return null;
  // Strip Genius/scraping artifacts: "N ContributorSong Title Lyrics\n"
  let cleaned=plainText.replace(/^\d+\s+Contributor[^\n]*\n?/,'');
  // Keep only real lyric lines — drop section headers [Intro]/[Chorus] and blank lines
  const lines=cleaned.split('\n')
    .map(l=>l.trim())
    .filter(l=>l&&!/^\[.*\]$/.test(l));
  if(!lines.length) return null;

  // Use vocal cue range when available (Traktor HOTCUE 2=Vocal In, 5=Vocal Out)
  // This distributes lines across the actual sung portion of the track.
  const hasVocalCues = vocalInMs>0 && vocalOutMs>0 && vocalOutMs>vocalInMs;
  const startMs = hasVocalCues ? vocalInMs  : 0;
  const endMs   = hasVocalCues ? vocalOutMs : durationMs;
  const span    = endMs - startMs;

  const step=span/lines.length;
  return lines.map((text,i)=>({ms:startMs+i*step,text}));
}

function getLyricLines(track){
  if(track&&track.lyrics_lrc){   _karMode='lrc'; return parseLRC(track.lyrics_lrc); }
  if(track&&track.lyrics_plain){
    _karMode='est';
    return estimateLines(track.lyrics_plain,track.duration_ms,
                         track.vocal_in_ms||0, track.vocal_out_ms||0);
  }
  _karMode=''; return null;  // no lyrics — panel stays hidden
}

function _karStatusText(elapsedMs){
  if(_karMode==='lrc')  return _karLive?'◉ LRC sync':'◎ LRC (waiting for position...)';
  if(_karMode==='est')  return _karLive?'◉ estimated':'◎ estimated (waiting for position...)';
  return '';
}

function _summaryHtml(t){
  // Restore the summary text for a given track (or blank if none)
  if(t&&t.lyric_summary) return`<div class="lyric-summary">♪ ${esc(t.lyric_summary)}</div>`;
  return'';
}

function updateKaraokeSource(){
  const el=document.getElementById('anc-lyric');
  if(!el||!anchor) return;

  // Which deck is the anchor on? Use play-state first, fall back to path match.
  const deck=deckPlaying.a?'a':deckPlaying.b?'b':
    (deckTracks.a&&deckTracks.a.path&&anchor.path&&deckTracks.a.path===anchor.path)?'a':
    (deckTracks.b&&deckTracks.b.path&&anchor.path&&deckTracks.b.path===anchor.path)?'b':null;

  // Always use the anchor track for karaoke — it IS the selected/playing track.
  if(anchor===_karAnchor&&_karLines) return;  // already set up for this anchor
  // Clear previous fallback timer
  if(_karTimerId){clearInterval(_karTimerId);_karTimerId=null;}
  _karAnchor=anchor; _karDeck=deck; _karLive=false;
  _karLines=getLyricLines(anchor);

  if(_karLines){
    _karWallStart=Date.now();
    el.innerHTML=`<div class="kl-status" id="kl-status"></div>`+
      `<div class="kl kl-prev3" id="kl-prev3"></div>`+
      `<div class="kl kl-prev2" id="kl-prev2"></div>`+
      `<div class="kl kl-prev"  id="kl-prev"></div>`+
      `<div class="kl kl-curr"  id="kl-curr"></div>`+
      `<div class="kl kl-next"  id="kl-next"></div>`+
      `<div class="kl kl-next2" id="kl-next2"></div>`+
      `<div class="kl kl-next3" id="kl-next3"></div>`;
    document.getElementById('kl-status').textContent=_karStatusText(0);
    renderKaraoke(_karLines,0);
    // Client-side fallback: advance display at 5 Hz until real position events arrive
    _karTimerId=setInterval(()=>{
      if(!_karLive&&_karLines){
        renderKaraoke(_karLines,Date.now()-_karWallStart);
      }
    },200);
  } else {
    el.innerHTML=_summaryHtml(anchor);
  }
}

function renderKaraoke(lines,elapsedMs){
  // Find current line: last line whose start time ≤ elapsed
  let idx=0;
  for(let i=0;i<lines.length;i++){
    if(lines[i].ms<=elapsedMs) idx=i; else break;
  }

  // Helper: nth non-blank line before/after idx (n=1 → nearest)
  function nearbyLine(dir,n){
    let found=0;
    for(let i=idx+dir;i>=0&&i<lines.length;i+=dir){
      if(lines[i].text&&++found===n) return lines[i].text;
    }
    return'';
  }

  // Previous three lines
  const prev3Text=nearbyLine(-1,3);
  const prev2Text=nearbyLine(-1,2);
  const prevText =nearbyLine(-1,1);

  // Current line — bold the active word when we have accurate timestamps (LRC mode)
  const curr=lines[idx];
  let currHtml=esc(curr.text);
  if(curr.text&&_karMode==='lrc'){
    // Find end of this line = start of next non-blank line
    let lineEndMs=elapsedMs+8000;
    for(let i=idx+1;i<lines.length;i++){if(lines[i].text){lineEndMs=lines[i].ms;break;}}
    const words=curr.text.split(' ');
    const pct=Math.min((elapsedMs-curr.ms)/Math.max(lineEndMs-curr.ms,1),1);
    const wi=Math.min(Math.floor(pct*words.length),words.length-1);
    currHtml=words.map((w,i)=>i===wi?`<b>${esc(w)}</b>`:esc(w)).join(' ');
  }

  // Next three lines
  const next1Text=nearbyLine(1,1);
  const next2Text=nearbyLine(1,2);
  const next3Text=nearbyLine(1,3);

  document.getElementById('kl-prev3').textContent=prev3Text;
  document.getElementById('kl-prev2').textContent=prev2Text;
  document.getElementById('kl-prev').textContent =prevText;
  document.getElementById('kl-curr').innerHTML   =currHtml;
  document.getElementById('kl-next').textContent =next1Text;
  document.getElementById('kl-next2').textContent=next2Text;
  document.getElementById('kl-next3').textContent=next3Text;
}

// ── Manual search ───────────────────────────────────────────────────────────
let st;
q.addEventListener('input',()=>{
  clearTimeout(st);
  if(q.value.trim().length<2){res.style.display='none';return}
  st=setTimeout(()=>doSearch(q.value.trim()),150);
});
q.addEventListener('keydown',e=>{
  if(e.key==='Enter'){
    const txt=q.value.trim();
    if(tryKeywordCommand(txt)){
      q.value='';
      res.style.display='none';
      clearTimeout(st);
      e.preventDefault();
    }
  }
});
document.addEventListener('click',e=>{if(!e.target.closest('#search-wrap'))res.style.display='none'});

async function doSearch(v){
  const d=await fetch('/api/search?q='+encodeURIComponent(v)).then(r=>r.json());
  if(!d.length){res.style.display='none';return}
  SR=d;
  res.innerHTML=d.map((t,i)=>`
    <div class="r" onclick="setAnchor(${i});copyTrack('${esc(t.artist)}','${esc(t.title)}')">
      <span style="flex:1"><span class="ra">${esc(t.artist)}</span><span style="color:#555"> — </span><span style="color:#fff">${esc(t.title)}</span></span>
      <span class="bpm">${t.bpm}</span><span class="key">${t.key||'—'}</span>
      <span class="gen" style="min-width:100px">${t.genre||'—'}</span>${stars(t.stars)}
    </div>`).join('');
  res.style.display='block';
}
async function setAnchor(i){res.style.display='none';q.value='';await loadAnchor(SR[i],null)}

// ── Load anchor (shared by OSC + manual) ───────────────────────────────────
async function loadAnchor(track,deck){
  anchor=track;
  const deckTag=deck?`<div class="deck-tag">SELECTED SONG ▸ DECK ${deck.toUpperCase()}</div>`:'<div class="deck-tag">SELECTED SONG</div>';
  // Replace anchor box only; keep deck cards
  const cardsEl=b1.querySelector('.deck-cards');
  const ancArt=track.art_url?`<img class="anc-art" src="${track.art_url}" onerror="this.style.display='none'" alt="">`:'';
  const badges=(repBadge(track)+instrBadge(track)+lyricBadges(track));
  const badgeRow=badges?`<div class="anchor-badges" style="margin-top:6px">${badges}</div>`:'';
  const boxCls='anchor-box'+(track.is_instrumental?' instrumental':'');
  const anchorHtml=`<div class="${boxCls}" data-track="${esc(JSON.stringify(track))}">${deckTag}${ancArt}
    <div class="an"><span class="aa">${esc(track.artist)}</span><span style="color:#555"> — </span><span class="at">${esc(track.title)}</span></div>
    ${meta(track,false)}${badgeRow}<div id="anc-lyric">${lyricLine(track)}</div></div>`;
  if(cardsEl){
    const ab=b1.querySelector('.anchor-box');
    if(ab) ab.outerHTML=anchorHtml;
    else b1.insertAdjacentHTML('afterbegin',anchorHtml);
    renderDeckCards();
  } else {
    b1.innerHTML=anchorHtml;
    renderDeckCards();
  }
  _karAnchor=null; _karDeck=null; updateKaraokeSource();  // re-inject into fresh #anc-lyric
  b2.innerHTML='<div class="empty">Loading…</div>';
  b3.innerHTML='<div class="empty">Loading…</div>';
  const deckParam=deck?'&deck='+encodeURIComponent(deck):'';
  const d=await fetch('/api/suggest?path='+encodeURIComponent(track.path)+deckParam).then(r=>r.json());
  S2=d.slot2;
  renderSlot2(0);
  renderSlot3(d.slot3);
  if(S2.length)slot2=S2[0];
  fitPassthrough();
}

// ── Slot 2 ──────────────────────────────────────────────────────────────────
function renderSlot2(selIdx){
  if(!S2.length){b2.innerHTML='<div class="empty">No close matches found</div>';return}
  b2.innerHTML=S2.map((t,i)=>tkHtml(t,i,i===selIdx,true)).join('');
  fitPassthrough();
}

// ── Dynamic auto-fit (passthrough only): shrink font-size per line until
// the text fits without wrapping or being cut off. Also shrinks cards if
// the vertical total exceeds the column body. Legibility floor = 9px.
// ── Size-justified text fit ──────────────────────────────────────────────
// Binary-search the largest font-size where the text's rendered rectangle
// fits its container with no overflow. Text fills the box end-to-end via
// text-align:justify for horizontal edges. Used for longer (sentence→paragraph)
// text blocks where we want them to fill their allotted rectangle exactly.
function sizeJustify(el, min=8, max=36, tol=0.25){
  if(!el||!el.textContent.trim())return;
  // Measure parent context — we want el's content to fit within its current box
  // Reset so we can measure
  el.style.fontSize='';
  const ch=el.clientHeight;
  const cw=el.clientWidth;
  if(ch<8||cw<8)return;  // no space, bail
  // Try max first — if it fits, we're done
  el.style.fontSize=max+'px';
  if(el.scrollHeight<=ch+1 && el.scrollWidth<=cw+1)return;
  // Binary search downward
  let lo=min, hi=max, best=min;
  for(let i=0;i<14 && hi-lo>tol;i++){
    const mid=(lo+hi)/2;
    el.style.fontSize=mid+'px';
    if(el.scrollHeight<=ch+1 && el.scrollWidth<=cw+1){best=mid;lo=mid;}
    else{hi=mid;}
  }
  el.style.fontSize=best+'px';
}

function fitPassthrough(){
  if(!document.body.classList.contains('passthrough'))return;
  requestAnimationFrame(()=>{
    // 1) Horizontal fit — shrink title/summary lines to avoid horizontal overflow
    const targets=document.querySelectorAll(
      'body.passthrough .tn, '+
      'body.passthrough .dc-name, '+
      'body.passthrough .anchor-box .an, '+
      'body.passthrough .tk .lyric-summary, '+
      'body.passthrough #deck-strip .ds-cell, '+
      'body.passthrough #selected-strip .ss-row'
    );
    targets.forEach(el=>{
      el.style.fontSize='';  // reset
      const cs=getComputedStyle(el);
      let fs=parseFloat(cs.fontSize);
      const minFs=el.classList.contains('lyric-summary')?9:10;
      let guard=40;
      while(el.scrollWidth>el.clientWidth+1 && fs>minFs && guard-->0){
        fs-=0.5;
        el.style.fontSize=fs+'px';
      }
    });
    // 2) Vertical fit — if any column body overflows, shrink all card fonts a step
    ['b2','b3'].forEach(id=>{
      const body=document.getElementById(id);
      if(!body)return;
      let guard=12;
      while(body.scrollHeight>body.clientHeight+1 && guard-->0){
        const cards=body.querySelectorAll('.tk');
        let stepped=false;
        cards.forEach(c=>{
          const cs=getComputedStyle(c);
          let fs=parseFloat(cs.fontSize);
          if(fs>10){c.style.fontSize=(fs-0.5)+'px';stepped=true}
          const meta=c.querySelector('.meta');
          if(meta){
            const mfs=parseFloat(getComputedStyle(meta).fontSize);
            if(mfs>9){meta.style.fontSize=(mfs-0.5)+'px';stepped=true}
          }
        });
        if(!stepped)break;
      }
    });
    // 3) Size-justified — fill allotted rectangle end-to-end. Runs LAST so
    //    the container has settled from earlier passes.
    document.querySelectorAll(
      'body.passthrough .anchor-box .lyric-summary, '+
      'body.passthrough .size-justified'
    ).forEach(el=>sizeJustify(el,10,28));
  });
}
// Refit on window resize
window.addEventListener('resize',()=>fitPassthrough());
async function pickSlot2(i){
  slot2=S2[i];renderSlot2(i);
  // Clear search bar — the selection is made, search context no longer needed
  const qEl=document.getElementById('q');
  if(qEl){qEl.value='';document.getElementById('results').style.display='none';}
  b3.innerHTML='<div class="empty">Loading…</div>';
  const d=await fetch('/api/slot3?slot2='+encodeURIComponent(slot2.path)+'&anchor='+encodeURIComponent(anchor.path)).then(r=>r.json());
  renderSlot3(d);
}

// ── Slot 3 ──────────────────────────────────────────────────────────────────
function renderSlot3(groups){
  if(!groups.length){b3.innerHTML='<div class="empty">No bridge candidates</div>';return}
  b3.innerHTML=groups.map(g=>`
    <div class="bg"><div class="bg-dest">→ ${esc(g.destination)}</div>
    ${g.tracks.map(t=>{const a=t.art_url?`<img class="art-thumb" src="${t.art_url}" loading="lazy" onerror="this.style.display='none'" alt="">`:''
      const c='tk'+(t.is_instrumental?' instrumental':'');
      return`<div class="${c}" data-track="${esc(JSON.stringify(t))}" onclick="copyTrack('${esc(t.artist)}','${esc(t.title)}')">
      ${a}<div class="tn" style="${t.art_url?'padding-right:42px':''}"><span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${instrBadge(t)}${lyricBadges(t)}</div>
      ${lyricLine(t)}${meta(t,true)}</div>`}).join('')}</div>`).join('');
  fitPassthrough();
}

// ── Drop-zone: add a new track to the library ────────────────────────────────
(function(){
  const zone   = document.getElementById('add-track-zone');
  const fileIn = document.getElementById('add-track-file');
  const status = document.getElementById('add-track-status');
  if(!zone) return;

  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('drag-over');
    const f = e.dataTransfer && e.dataTransfer.files[0];
    if(f) _uploadTrack(f);
  });
  zone.addEventListener('click', e => {
    if(e.target !== fileIn) fileIn.click();
  });
  fileIn.addEventListener('change', e => {
    if(e.target.files[0]) _uploadTrack(e.target.files[0]);
    fileIn.value = '';
  });

  function _setStatus(msg, isError){
    status.textContent = msg;
    status.className = isError ? 'error' : '';
  }

  function _uploadTrack(file){
    _setStatus('Uploading ' + file.name + '…');
    const form = new FormData();
    form.append('file', file);
    fetch('/api/add_track', {method:'POST', body:form})
      .then(r => r.json())
      .then(d => {
        if(d.job_id){
          _streamProgress(d.job_id);
        } else {
          _setStatus('Error: ' + (d.error || 'unknown'), true);
        }
      })
      .catch(err => _setStatus('Upload failed: ' + err, true));
  }

  function _streamProgress(jobId){
    const es = new EventSource('/api/add_track_progress?id=' + jobId);
    es.onmessage = e => {
      try {
        const d = JSON.parse(e.data);
        _setStatus(d.msg, !d.ok && d.done);
        if(d.done){
          es.close();
          if(d.ok){
            setTimeout(() => {
              _setStatus('');
              // Nudge the server to reload its NML (best-effort)
              fetch('/api/reload_collection', {method:'POST'}).catch(()=>{});
            }, 2000);
          }
        }
      } catch(_){}
    };
    es.onerror = () => { es.close(); _setStatus('Connection lost', true); };
  }
})();
</script>
</body>
</html>"""


SETLIST_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tonight's Setlist</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#050505;color:#ccc;font-family:'Courier New',Courier,monospace;padding:32px 24px;max-width:640px;margin:0 auto}
h1{color:#6ee7b7;font-size:18px;letter-spacing:3px;text-transform:uppercase;margin-bottom:4px}
#subtitle{color:#444;font-size:11px;letter-spacing:1px;margin-bottom:28px}
#count{color:#555;font-size:11px;margin-bottom:20px}
ol{list-style:none;padding:0}
ol li{display:grid;grid-template-columns:28px 44px 1fr 80px;align-items:baseline;gap:8px;padding:8px 0;border-bottom:1px solid #111;font-size:13px}
.num{color:#333;text-align:right;font-size:11px}
.time{color:#555;font-size:11px}
.track{color:#ccc}
.track .artist{font-weight:700}
.track .sep{color:#444}
.track .title{color:#888}
.genre{color:#444;font-size:10px;text-align:right;letter-spacing:.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#empty{color:#444;font-style:italic;text-align:center;padding:60px 0;font-size:13px}
#footer{margin-top:28px;color:#333;font-size:10px;letter-spacing:1px}
</style>
</head>
<body>
<h1>📋 Tonight's Setlist</h1>
<div id="subtitle">Auto-refreshes every 15 seconds</div>
<div id="count"></div>
<ol id="list"></ol>
<div id="empty" style="display:none">Waiting for first track…</div>
<div id="footer">localhost:7334/setlist · Mac Bilby</div>
<script>
async function load(){
  const r=await fetch('/api/setlist');
  const d=await r.json();
  const sl=d.setlist||[];
  document.getElementById('count').textContent=sl.length?`${sl.length} track${sl.length>1?'s':''} played`:'';
  const ol=document.getElementById('list');
  const em=document.getElementById('empty');
  ol.innerHTML='';
  if(!sl.length){em.style.display='';return;}
  em.style.display='none';
  sl.forEach((e,i)=>{
    const li=document.createElement('li');
    li.innerHTML=`<span class="num">${i+1}</span>`
      +`<span class="time">${e.played_at||''}</span>`
      +`<span class="track"><span class="artist">${e.artist}</span><span class="sep"> — </span><span class="title">${e.title}</span></span>`
      +`<span class="genre">${e.genre||''}</span>`;
    ol.appendChild(li);
  });
}
load();
setInterval(load,15000);
</script>
</body>
</html>"""


def make_app(tracks: list[Track], osc_state: OSCState, osc_on: bool):
    from flask import Flask, Response, jsonify, request, stream_with_context, send_from_directory

    app   = Flask(__name__)
    index = {t.path: t for t in tracks}

    @app.route("/")
    def ui():
        return Response(HTML, mimetype="text/html")

    @app.route("/manifest.json")
    def suip_manifest():
        """SUIP v1 manifest — fetched by Skippy after registration.
        Give SkippyView: http://<mac-tailscale-host>:7334/manifest.json"""
        from tools.suip_scene import build_manifest
        return jsonify(build_manifest()), 200, {"Cache-Control": "no-cache"}

    @app.route("/.well-known/skippy-passthrough.json")
    def suip_manifest_legacy():
        """Legacy alias — redirects to /manifest.json."""
        from flask import redirect
        return redirect("/manifest.json", code=301)

    @app.route("/control")
    def suip_control_stream():
        """SSE endpoint declared as stream_url in SUIP registration.
        Skippy may open GET /control; we send a connected event then keepalives.
        Scene patches flow via POST to Skippy's /passthrough/patch as normal."""
        def _gen():
            import time as _t
            yield "data: {\"type\":\"connected\"}\n\n"
            while True:
                _t.sleep(15)
                yield ": keepalive\n\n"
        return Response(
            _gen(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/setlist")
    def setlist_page():
        """
        Standalone setlist page — open in another tab or on a second screen.
        Auto-refreshes every 15 seconds so other DJs can follow along live.
        Shows tonight's played tracks with timestamp, artist, title, and genre.
        """
        return Response(SETLIST_PAGE_HTML, mimetype="text/html")

    @app.route("/api/count")
    def count():
        return jsonify({"count": len(tracks), "osc": osc_on,
                        "playing_deck": osc_state.playing_deck()})

    @app.route("/api/now-playing")
    def now_playing():
        deck = osc_state.playing_deck()
        return jsonify({"deck": deck})

    @app.route("/api/save-me")
    def save_me():
        """
        Return the single highest-rated floor track closest to the current anchor.
        Prefers same/neighbouring genre and tight BPM match. Guaranteed dancefloor.
        """
        import random
        anchor_path = request.args.get("anchor", "")
        anchor = index.get(anchor_path)

        def score(t: Track) -> float:
            if t.stars < 3: return -1
            if t.genre not in FLOOR_GENRES and anchor is None: return -1
            star_w = t.stars / 5.0
            bpm_w  = bpm_compat(anchor.bpm, t.bpm)  if anchor else 0.5
            gen_w  = genre_compat(anchor.genre, t.genre) if anchor else (
                     1.0 if t.genre in FLOOR_GENRES else 0.0)
            return 0.4 * star_w + 0.35 * bpm_w + 0.25 * gen_w

        candidates = sorted(
            [(score(t), t) for t in tracks if score(t) > 0],
            key=lambda x: -x[0]
        )
        if not candidates:
            return jsonify({"error": "no candidates"}), 404
        # Pick best from top-5 (slight randomness so it's not always identical)
        pick_score, pick = random.choice(candidates[:5])
        return jsonify(pick.to_dict(pick_score))

    @app.route("/api/surprise-me")
    def surprise_me():
        """
        Return a highly-rated floor track NOT played this session.
        Random pick from top 20 so it's actually surprising.
        """
        import random
        played = _get_played()

        def score(t: Track) -> float:
            if t.path in played: return -1
            if t.stars < 3:     return -1
            if t.genre not in FLOOR_GENRES: return -1
            return t.stars / 5.0 + random.random() * 0.15   # shuffle within tier

        candidates = sorted(
            [(score(t), t) for t in tracks if score(t) > 0],
            key=lambda x: -x[0]
        )
        if not candidates:
            return jsonify({"error": "no unplayed candidates"}), 404
        pick_score, pick = random.choice(candidates[:20])
        return jsonify(pick.to_dict(pick_score))

    @app.route("/api/swap-decks", methods=["POST"])
    def swap_decks():
        osc_state.swap_decks()
        return jsonify({"ok": True})

    @app.route("/api/input-text", methods=["POST"])
    def input_text():
        """Inject text into the browser search box as if typed + Enter pressed.
        Broadcasts via SSE to every connected browser. If the text matches a
        keyword command (e.g. 'swap decks', 'select 3'), it fires that command;
        otherwise it drops into the search box and triggers a live search.

        Used by external DJ services — OCR, voice-to-text, remote control.
        The end goal is full solo-glasses DJing: you talk, the glasses or a
        bridge app transcribes, that POSTs here, the browser reacts.

        curl -X POST http://localhost:7334/api/input-text \\
             -H 'Content-Type: application/json' \\
             -d '{"text":"swap decks"}'
        """
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or request.args.get("text") or "").strip()
        if not text:
            return jsonify({"error": "missing text"}), 400
        osc_state.broadcast_input(text)
        return jsonify({"ok": True, "text": text})

    @app.route("/api/load-to-deck", methods=["POST"])
    def load_to_deck():
        """Write a 1-track M3U for the given deck. Mirrors CLI 1-5/q-t shortcut."""
        deck = request.args.get("deck", "").lower()
        path = request.args.get("path", "")
        if deck not in ("a", "b"):
            return jsonify({"error": "bad deck"}), 400
        t = index.get(path)
        if not t:
            return jsonify({"error": "track not in collection"}), 404
        SUGGESTIONS_DIR.mkdir(exist_ok=True)
        out = SUGGESTIONS_DIR / f"deck_{deck}.m3u"
        label = f"{t.artist} — {t.title}  [{t.bpm:.1f} BPM | {t.key} | {t.genre}]"
        out.write_text(f"#EXTM3U\n#EXTINF:-1,{label}\n{path}\n", encoding="utf-8")
        return jsonify({"ok": True, "deck": deck, "path": path})

    @app.route("/api/deck-status")
    def deck_status():
        loaded  = osc_state.get_loaded()   # {deck: {title, artist}}
        playing = osc_state.get_playing()  # {deck: bool}
        return jsonify({
            "a": loaded.get("a"),
            "b": loaded.get("b"),
            "playing_a": playing.get("a", False),
            "playing_b": playing.get("b", False),
        })

    # ── SkippyTel / Bilby service endpoints ──────────────────────────────────
    # SkippyTel (skippy-pc) knows about Mac Bilby via base_url in its service
    # manifest. These routes let SkippyTel probe availability and let the zPhone
    # query now-playing state and request track changes via voice commands.

    @app.route("/bilby/status")
    def bilby_status():
        """Now-playing state. SkippyTel probes this to detect Mac online/offline."""
        deck    = osc_state.playing_deck()
        loaded  = osc_state.get_loaded()
        playing = osc_state.get_playing()
        host    = request.host  # e.g. skippy-mac:7334

        now = None
        if deck and loaded.get(deck):
            info  = loaded[deck]
            # Enrich with full Track data if we can find it in the index
            title_q  = (info.get("title",  "") or "").lower()
            artist_q = (info.get("artist", "") or "").lower()
            t = next((tr for tr in tracks
                      if title_q in tr.title.lower() and artist_q in tr.artist.lower()), None)
            elapsed   = osc_state.get_elapsed(deck)
            now = {
                "deck":          deck,
                "artist":        info.get("artist", ""),
                "title":         info.get("title", ""),
                "bpm":           round(t.bpm, 1) if t else None,
                "key":           t.key if t else None,
                "genre":         t.genre if t else None,
                "duration_s":    t.duration if t else None,
                "elapsed_s":     round(elapsed, 2),
                "lyric_summary": t.comment if t else None,
                "art_url":       (f"http://{host}{t.art_url}" if t and t.art_url else None),
            }

        return jsonify({
            "spec_version": "1",
            "mode":         "traktor",
            "online":       True,
            "now_playing":  now,
            "deck_a":       loaded.get("a"),
            "deck_b":       loaded.get("b"),
            "playing_a":    playing.get("a", False),
            "playing_b":    playing.get("b", False),
        })

    @app.route("/bilby/next", methods=["POST", "GET"])
    def bilby_next():
        """Voice command: 'next track' / 'bilby next'. Returns top slot2 suggestion."""
        with _SUGG_LOCK:
            s2 = list(_SUGG_STATE.get("slot2", []))
        if not s2:
            return jsonify({"spec_version": "1", "ok": False,
                            "message": "No suggestions ready — load a track first."})
        top = s2[0]
        return jsonify({
            "spec_version": "1",
            "ok":      True,
            "message": f"Up next: {top.get('artist','?')} — {top.get('title','?')}",
            "track":   top,
        })

    @app.route("/bilby/stop", methods=["POST", "GET"])
    def bilby_stop():
        """Voice command: 'stop music'. Acknowledged — Traktor OSC control is future work."""
        return jsonify({
            "spec_version": "1",
            "ok":      True,
            "message": "Stop acknowledged — control Traktor directly to halt playback.",
        })

    @app.route("/api/search")
    def search():
        q = request.args.get("q", "").lower().strip()
        if len(q) < 2: return jsonify([])
        return jsonify([t.to_dict() for t in tracks if q in t.search_text][:30])

    @app.route("/api/suggest")
    def suggest():
        t    = index.get(request.args.get("path", ""))
        deck = request.args.get("deck", "").strip().lower() or None
        if not t: return jsonify({"error": "not found"}), 404
        s2  = suggest_slot2(t, tracks)
        ref = index.get(s2[0]["path"]) if s2 else t
        s3  = suggest_slot3(ref, t, tracks)
        try:
            print_suggestions(deck, t, s2, s3)
        except Exception:
            pass  # never let terminal I/O crash the API
        if deck in ("a", "b"):
            try:
                write_m3u(deck, t, s2, s3)
            except Exception:
                pass   # never let playlist I/O crash the API
        return jsonify({"anchor": t.to_dict(), "slot2": s2, "slot3": s3})

    @app.route("/api/slot3")
    def slot3():
        s2t = index.get(request.args.get("slot2",  ""))
        anc = index.get(request.args.get("anchor", ""))
        if not s2t or not anc: return jsonify([])
        return jsonify(suggest_slot3(s2t, anc, tracks))

    @app.route("/api/resolve-deck")
    def resolve_deck():
        title  = request.args.get("title",  "").lower().strip()
        artist = request.args.get("artist", "").lower().strip()
        best, best_score = None, 0
        for t in tracks:
            tl = t.title.lower()
            ar = t.artist.lower()
            if title and artist and title in tl and artist in ar:
                score = 3
            elif title and artist and (title in t.search_text or artist in t.search_text):
                score = 2
            elif title and title in tl:
                score = 1
            else:
                continue
            if score > best_score:
                best_score, best = score, t
        return jsonify(best.to_dict() if best else None)

    @app.route("/art/<filename>")
    def serve_art(filename):
        """Serve cached album art JPEG files."""
        return send_from_directory(ART_DIR, filename)

    @app.route("/api/reload-art", methods=["POST"])
    def reload_art():
        """Hot-reload art index without server restart (call while fetch_album_art.py runs)."""
        global ART_INDEX
        ART_INDEX = _load_art_index(ART_INDEX_PATH)
        found = sum(1 for v in ART_INDEX.values() if v)
        return jsonify({"ok": True, "total": len(ART_INDEX), "found": found})

    @app.route("/api/reload-lyrics", methods=["POST"])
    def reload_lyrics():
        """Hot-reload lyrics index without server restart. Call after running stage9_lyrics.py."""
        global LYRICS
        LYRICS = load_lyrics_index(LYRICS_INDEX)
        return jsonify({"loaded": len(LYRICS), "flagged": sum(1 for v in LYRICS.values() if v.get("flags"))})

    @app.route("/api/show-config", methods=["GET", "POST"])
    def show_config():
        """
        GET  → return current SHOW_GENRES list (null = open floor / no filter)
        POST → set SHOW_GENRES.  Body: {"genres": [...]} or {"genres": null}
        """
        global SHOW_GENRES
        if request.method == "POST":
            body = request.get_json(force=True)
            genres = body.get("genres")
            if genres is None:
                SHOW_GENRES = None
            else:
                SHOW_GENRES = set(genres)
            label = "Open Floor" if SHOW_GENRES is None else f"{len(SHOW_GENRES)} genres"
            print(f"  [show-config] SHOW_GENRES set → {label}")
        return jsonify({
            "genres": sorted(SHOW_GENRES) if SHOW_GENRES is not None else None
        })

    @app.route("/api/setlist", methods=["GET", "DELETE"])
    def setlist():
        """
        GET    → return current show setlist as JSON
        DELETE → reset show: clears setlist, played paths, and played artists
        """
        if request.method == "DELETE":
            _reset_show()
            return jsonify({"status": "reset", "message": "Show reset — played history cleared"})
        sl = _get_setlist()
        return jsonify({"count": len(sl), "setlist": sl})

    @app.route("/api/export-setlist")
    def export_setlist():
        """
        Return the setlist in a social-media-ready plain text format,
        plus an M3U block for use in Traktor / media players.
        """
        import datetime
        sl   = _get_setlist()
        date = datetime.date.today().strftime("%B %d, %Y")
        lines = [f"🎵 DJ Set — {date}", "─" * 40]
        for i, entry in enumerate(sl, 1):
            time_str = f"[{entry['played_at']}]" if entry.get("played_at") else ""
            lines.append(f"{i:02d}. {entry['artist']} — {entry['title']}  {time_str}")
        lines += ["─" * 40, "#goth #darkwave #industrial #dj", ""]

        # M3U block
        lines.append("#EXTM3U")
        for entry in sl:
            lines.append(f"#EXTINF:-1,{entry['artist']} — {entry['title']}")
        text = "\n".join(lines)
        return text, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/api/activity")
    def activity():
        """Return current background task progress, or null if idle."""
        if ACTIVITY_FILE.exists():
            try:
                return jsonify(json.loads(ACTIVITY_FILE.read_text()))
            except Exception:
                pass
        return jsonify(None)

    @app.route("/api/lyrics-batch")
    def lyrics_batch():
        """
        Serve the current lyrics batch export for the analysis PC to pull.
        The PC hits this endpoint, processes with its powerful model,
        then POSTs results to /api/lyrics-results.
        """
        batch_file = BASE / "state" / "lyrics_batch_export.json"
        if not batch_file.exists():
            return jsonify({"error": "No batch file. Run export_lyrics_for_analysis.py first."}), 404
        return jsonify(json.loads(batch_file.read_text()))

    @app.route("/api/lyrics-results", methods=["POST"])
    def lyrics_results():
        """
        Receive analysis results from the PC.
        Body: {"dkey": {"summary": "...", "flags": [...]}, ...}
        Merges into dedup cache and index, then hot-reloads.
        """
        global LYRICS
        import re

        def base_title(t):
            return re.sub(r'\s*[\(\[].{0,40}[\)\]]\s*$', "", t).strip().lower()
        def dedup_key(a, t):
            return f"{a.lower().strip()}\t{base_title(t)}"

        results = request.get_json(force=True) or {}
        dedup_file = BASE / "state" / "lyrics_dedup.json"
        dedup = json.loads(dedup_file.read_text()) if dedup_file.exists() else {}
        index_file = BASE / "state" / "lyrics_index.json"
        index = json.loads(index_file.read_text()) if index_file.exists() else {}

        merged = flagged = 0
        for dkey, entry in results.items():
            if not entry.get("summary"):
                continue
            dedup[dkey] = {"summary": entry["summary"], "flags": entry.get("flags", [])}
            if entry.get("flags"):
                flagged += 1
            merged += 1

        # Propagate dkey → path mappings through the in-memory track list
        for t in tracks:
            dk = dedup_key(t.artist, t.title)
            if t.path not in index and dk in dedup:
                index[t.path] = dedup[dk]

        dedup_file.write_text(json.dumps(dedup, ensure_ascii=False))
        index_file.write_text(json.dumps(index, ensure_ascii=False))
        LYRICS = load_lyrics_index(index_file)

        return jsonify({"merged": merged, "flagged": flagged, "total_indexed": len(index)})

    @app.route("/api/events")
    def events():
        q = queue.Queue()
        osc_state.add_client(q)
        def generate():
            try:
                yield 'data: {"type":"connected"}\n\n'
                while True:
                    try:
                        event = q.get(timeout=25)
                        yield f"data: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                osc_state.remove_client(q)
        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Drop-zone: add a new track to the library ─────────────────────────────
    import uuid as _uuid
    import tempfile as _tempfile
    import threading as _threading
    import queue as _queue_mod

    _ADD_TRACK_JOBS: dict = {}

    @app.route("/api/add_track", methods=["POST"])
    def add_track_upload():
        """Accept a dropped audio file and kick off the full ingestion pipeline."""
        f = request.files.get("file")
        if not f:
            return jsonify(error="no file"), 400

        suffix = Path(f.filename).suffix if f.filename else ".mp3"
        job_id = str(_uuid.uuid4())[:8]
        q: _queue_mod.Queue = _queue_mod.Queue()
        _ADD_TRACK_JOBS[job_id] = q

        # Save to temp file (Flask file object exhausted after save)
        tmp = _tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.save(tmp.name)
        tmp_path = Path(tmp.name)

        def _run():
            try:
                sys.path.insert(0, str(BASE / "tools"))
                from tools.add_track import add_track as _add_track
                _add_track(tmp_path, progress_queue=q)
            except Exception as ex:
                q.put({"msg": f"Internal error: {ex}", "done": True, "ok": False})
            finally:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        _threading.Thread(target=_run, daemon=True, name="add-track").start()
        return jsonify(job_id=job_id)

    @app.route("/api/add_track_progress")
    def add_track_progress():
        """SSE stream for add_track job progress."""
        job_id = request.args.get("id", "")
        q = _ADD_TRACK_JOBS.get(job_id)
        if not q:
            return jsonify(error="unknown job"), 404

        def _generate():
            while True:
                try:
                    msg = q.get(timeout=30)
                except _queue_mod.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("done"):
                    _ADD_TRACK_JOBS.pop(job_id, None)
                    break

        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.route("/api/reload_collection", methods=["POST"])
    def reload_collection():
        """Hot-reload the track collection from NML (called after add_track completes)."""
        try:
            new_tracks = load_tracks(TRAKTOR_NML)
            tracks.clear()
            tracks.extend(new_tracks)
            return jsonify(ok=True, count=len(tracks))
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 500

    return app


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    print("Mac Bilby — yeah, loading the collection, one sec…", end=" ", flush=True)
    tracks = load_tracks(TRAKTOR_NML)
    print(f"{len(tracks):,} tracks. Nice.")

    osc_state = OSCState()
    osc_on    = start_osc_server(osc_state, OSC_PORT)

    if osc_on:
        print(f"  OSC up on {OSC_PORT} — Traktor's in range, we're good.")
    else:
        print(f"  OSC not available on {OSC_PORT} — running manual mode, no biggie.")

    SUGGESTIONS_DIR.mkdir(exist_ok=True)
    print(f"\n  Browser:   http://localhost:{PORT}")
    print(f"  M3U:       {SUGGESTIONS_DIR}/deck_a.m3u  |  deck_b.m3u")
    print(f"\n  Keys:  1–5 = load Lock N → Deck A   q–t = Deck B   x = quit")
    print(f"  Traktor setup (one-time): Controller Manager → Keyboard →")
    print(f"    Ctrl+1 → Deck A → Load Selected Track")
    print(f"    Ctrl+2 → Deck B → Load Selected Track\n")

    app   = make_app(tracks, osc_state, osc_on)

    # ── lsof deck watcher — detects Traktor track loads automatically ─────────
    index = {t.path: t for t in tracks}
    start_lsof_watcher(tracks, index, osc_state)
    print(f"  Deck watcher: polling Traktor every 2s via lsof")

    # ── History watcher — track info + position via Traktor history NML ───────
    # Traktor Pro 4 removed Generic OSC; we watch the history file instead.
    hw = HistoryWatcher(osc_state, index)
    hw.start()
    print(f"  History watcher: watching {HistoryWatcher.HISTORY_DIR.name}/")

    flask_thread = threading.Thread(
        target=lambda: app.run(host=os.environ.get("STAGE9_HOST", "0.0.0.0"), port=PORT,
                               debug=False, use_reloader=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    # ── SUIP client — opt-in via SKIPPY_URL env var ─────────────────────────
    skippy_url = os.environ.get("SKIPPY_URL", "").strip()
    if skippy_url:
        try:
            import logging as _log
            _log.basicConfig(level=_log.INFO)
            sys.path.insert(0, str(BASE / "tools"))
            from suip_client import SUIPClient, IntentHandler
            from suip_scene import DJState

            default_manifest = f"http://127.0.0.1:{PORT}/manifest.json"
            manifest_url = os.environ.get("SUIP_MANIFEST_URL", default_manifest)

            _suip_handler = IntentHandler(
                osc_state       = osc_state,
                index           = index,
                tracks          = tracks,
                suggestions_dir = SUGGESTIONS_DIR,
                suggest_slot2_fn= suggest_slot2,
                suggest_slot3_fn= suggest_slot3,
                update_sugg_state_fn = _update_sugg_state,
            )

            def _get_state() -> DJState:
                with _SUGG_LOCK:
                    a = _SUGG_STATE["anchor"]
                    a_dict = a.to_dict() if a is not None else None
                    s2 = list(_SUGG_STATE["slot2"])
                    s3 = list(_SUGG_STATE["slot3"])
                # Observe for IntentHandler's pick-state logic
                _suip_handler.observe_state(a, s2, s3)
                return DJState(
                    anchor=a_dict,
                    slot2=s2,
                    slot3=s3,
                    deck_loaded=osc_state.get_loaded(),
                    deck_playing=osc_state.get_playing(),
                    selected_slot2_idx=_suip_handler.selected_idx(),
                )

            _suip = SUIPClient(skippy_url, manifest_url, _get_state, _suip_handler)
            _SUIP_HOOKS.append(_suip.notify_state_changed)
            _suip.start()
            import atexit
            atexit.register(_suip.stop)
            print(f"  Streaming to SkippyTel → {skippy_url}")
            print(f"  Manifest   → {manifest_url}  (zPhone can see us)")
        except Exception as ex:
            print(f"  SUIP client failed to start: {ex}")
    else:
        print(f"  SkippyTel: /bilby/status ready — add 'skippy-mac' → 100.93.161.25 on PC hosts.")

    run_key_listener()   # blocks main thread; x / Ctrl+C exits


if __name__ == "__main__":
    main()
