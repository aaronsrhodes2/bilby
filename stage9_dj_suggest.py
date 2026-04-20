#!/usr/bin/env python3
"""
Stage 9 — DJ Block Planner  (live, floor-reactive)

Watches Deck A and Deck B via Traktor's OSC output. The moment you load
a track, suggestions appear automatically — no typing, no clicking.
Falls back to manual search if OSC is not configured.

Run:   python3 stage9_dj_suggest.py
Open:  http://localhost:5001

── Traktor OSC setup (one-time) ─────────────────────────────────────────────
Traktor Preferences → Controller Manager → Add → Generic OSC

  Device name:   DJ Suggester
  Out-Port:      9000
  Out-IP:        127.0.0.1

Add six OUT mappings (Type: Output, each one):

  Control: Track > Title     Deck: Deck A   OSC Address: /deck/a/title
  Control: Track > Artist    Deck: Deck A   OSC Address: /deck/a/artist
  Control: Deck > Play       Deck: Deck A   OSC Address: /deck/a/play
  Control: Track > Title     Deck: Deck B   OSC Address: /deck/b/title
  Control: Track > Artist    Deck: Deck B   OSC Address: /deck/b/artist
  Control: Deck > Play       Deck: Deck B   OSC Address: /deck/b/play

Save and close Preferences. Traktor will now broadcast track info here.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import queue
import subprocess
import sys
import termios
import threading
import time
import tty
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE            = Path(__file__).parent
TRAKTOR_NML     = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
SUGGESTIONS_DIR = BASE / "suggestions"
REP_FLAGS_FILE  = BASE / "misc" / "reputation_flags.json"
LYRICS_INDEX    = BASE / "state" / "lyrics_index.json"
ACTIVITY_FILE   = BASE / "state" / "activity.json"
PORT            = 7334
OSC_PORT        = 9000

# ── Data model ────────────────────────────────────────────────────────────────

RANKING_TO_STARS = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}

@dataclass
class Track:
    path:     str
    artist:   str
    title:    str
    bpm:      float
    key:      str
    genre:    str
    stars:    int
    duration: float = 0.0   # seconds, from NML PLAYTIME
    comment:  str   = ""    # NML INFO COMMENT — lyric summary written by write_nml_comments.py

    @property
    def search_text(self) -> str:
        return f"{self.artist} {self.title}".lower()

    def to_dict(self, score: float = 0.0, transition: str = "") -> dict:
        rep  = reputation_for(self.artist)
        lyr  = lyrics_for(self.path)
        sflag = song_flag_for(self.artist, self.title)
        return {
            "path":         self.path,
            "artist":       self.artist,
            "title":        self.title,
            "bpm":          round(self.bpm, 1),
            "key":          self.key,
            "genre":        self.genre,
            "stars":        self.stars,
            "score":        round(score * 100),
            "transition":   transition,
            "rep_tier":     rep["tier"]     if rep else None,
            "rep_summary":  rep["summary"]  if rep else None,
            "song_flag":    sflag,
            "lyric_summary":lyr["summary"]  if lyr else (self.comment or None),
            "lyric_theme":  lyr["theme"]    if lyr else None,
            "lyric_flags":  lyr["flags"]    if lyr else [],
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


# ── Lyrics index ──────────────────────────────────────────────────────────────

def load_lyrics_index(path: Path) -> dict[str, dict]:
    """Load lyrics summary+flags cache. Returns {} if not yet built."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

LYRICS: dict[str, dict] = load_lyrics_index(LYRICS_INDEX)

def lyrics_for(path: str) -> dict | None:
    """Return {summary, flags} for a track path, or None."""
    entry = LYRICS.get(path)
    if not entry or not entry.get("summary"):
        return None
    return entry


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
        raw.append((bitrate, Track(
            path     = path,
            artist   = artist,
            title    = title,
            bpm      = bpm,
            key      = info.get("KEY",   ""),
            genre    = info.get("GENRE", ""),
            stars    = RANKING_TO_STARS.get(ranking, 0),
            duration = duration,
            comment  = info.get("COMMENT", "") or "",
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

# How long a track must stay in a deck (beyond the other deck's song length)
# before we consider it "played".  2 min gives time to preview without false-positives.
PLAYED_GUARD_SECS  = 120   # added on top of other deck's duration
SOLO_PLAYED_SECS   = 90    # threshold when no other-deck duration is available

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


def _update_sugg_state(slot2: list, slot3: list, anchor) -> None:
    with _SUGG_LOCK:
        _SUGG_STATE["slot2"]  = slot2
        _SUGG_STATE["slot3"]  = slot3
        _SUGG_STATE["anchor"] = anchor


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
      If it stayed longer than (other deck's track duration + PLAYED_GUARD_SECS),
      it was almost certainly played — not just previewed and swapped.
      Confirmed-played tracks land in _SETLIST for the post-show export.
    """
    # path → deck letter
    deck_map:   dict[str, str]           = {}
    # path → time.time() when first seen by lsof
    load_times: dict[str, float]         = {}
    # deck → Track currently in that deck (for duration lookup)
    deck_track: dict[str, object]        = {}   # Track objects

    def _resolve(fpath: str):
        track = index.get(fpath)
        if track is None:
            bn    = Path(fpath).name
            track = next((t for t in tracks if Path(t.path).name == bn), None)
        return track

    def _check_played(fpath: str, deck: str) -> None:
        """Decide whether a departing track was actually played."""
        load_time = load_times.pop(fpath, None)
        if load_time is None:
            return
        time_open = time.time() - load_time
        # Get the other deck's current track to estimate how long the set was running
        other_deck = "b" if deck == "a" else "a"
        other      = deck_track.get(other_deck)
        if other and getattr(other, "duration", 0) > 30:
            threshold = other.duration + PLAYED_GUARD_SECS
        else:
            threshold = SOLO_PLAYED_SECS
        if time_open >= threshold:
            track = _resolve(fpath)
            if track:
                _mark_played_track(track)

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

            # Check departing files for played detection BEFORE updating deck_map
            for fpath in gone_files:
                deck = deck_map.get(fpath)
                if deck:
                    _check_played(fpath, deck)

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
        self._sse_qs  = []        # SSE client queues

    def _push(self, event: dict) -> None:
        """Send an event to all connected SSE clients (must hold lock)."""
        for q in list(self._sse_qs):
            try: q.put_nowait(event)
            except: pass

    def on_message(self, deck: str, field: str, value: str):
        with self._lock:
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


# ── Flask app ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DJ Block Planner</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
/* ── Dark mode (default) ── */
:root{
  --bg:        #111;
  --bg2:       #0d0d1a;
  --bg3:       #0a0a0a;
  --bg4:       #1a1a1a;
  --border:    #1a1a1a;
  --border2:   #2d2d2d;
  --text:      #ddd;
  --text2:     #888;
  --text3:     #555;
  --text4:     #444;
  --accent:    #e63946;
  --card-bg:   #161616;
  --card-sel:  #1a1a2e;
  --card-bdr:  #2a2a4a;
  --meta:      #666;
  --lyric:     #4a5568;
}
/* ── Light mode (outdoor) ── */
body.light{
  --bg:        #f0ede8;
  --bg2:       #e8e4de;
  --bg3:       #e2ddd7;
  --bg4:       #d8d3cc;
  --border:    #c8c3bc;
  --border2:   #b8b3ac;
  --text:      #1a1a1a;
  --text2:     #444;
  --text3:     #666;
  --text4:     #888;
  --accent:    #c0392b;
  --card-bg:   #ebe7e1;
  --card-sel:  #ddd8f0;
  --card-bdr:  #9b8ec4;
  --meta:      #555;
  --lyric:     #6b7280;
}
body{background:var(--bg);color:var(--text);font-family:'Atkinson Hyperlegible','Courier New',monospace;font-size:13px;height:100vh;display:flex;flex-direction:column;transition:background .2s,color .2s}
#hdr{background:var(--bg2);padding:10px 18px;border-bottom:2px solid var(--accent);display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{color:var(--accent);font-size:15px;letter-spacing:3px;text-transform:uppercase;flex:1}
#hdr small{color:var(--text2);font-size:11px}
/* Theme toggle button */
#theme-btn{background:transparent;border:1px solid var(--border2);color:var(--text2);padding:3px 8px;border-radius:3px;font-family:inherit;font-size:12px;cursor:pointer;letter-spacing:.5px;flex-shrink:0}
#theme-btn:hover{border-color:var(--text2);color:var(--text)}
#osc-status{font-size:10px;padding:3px 8px;border-radius:3px;letter-spacing:1px;text-transform:uppercase}
#osc-status.on{background:#14532d;color:#4ade80}
#osc-status.off{background:#1e293b;color:#555}
#deck-bar{background:#0a0a0a;border-bottom:1px solid #1a1a1a;padding:6px 18px;display:flex;gap:12px;align-items:center;flex-shrink:0;min-height:34px}
.deck-pill{font-size:10px;padding:3px 10px;border-radius:12px;letter-spacing:1px;text-transform:uppercase;border:1px solid #222;color:#444}
.deck-pill.loaded{border-color:#555;color:#888;background:#1a1a1a}
.deck-pill.playing{border-color:#e63946;color:#e63946;background:#1a0808;box-shadow:0 0 6px #e6394644}
#deck-msg{color:#555;font-size:11px;flex:1}
#swap-btn{background:#1a1a1a;color:#777;border:1px solid #333;padding:3px 10px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;letter-spacing:1px}
#swap-btn:hover{border-color:#888;color:#ccc;background:#222}
.panic-btn{border:none;padding:5px 13px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px;transition:opacity .1s}
#save-btn{background:#7f1d1d;color:#fca5a5}#save-btn:hover{background:#991b1b}
#surprise-btn{background:#1e3a5f;color:#93c5fd}#surprise-btn:hover{background:#1d4ed8}
#show-btn{background:#1a1a1a;color:#a78bfa;border:1px solid #3b2d6e;padding:5px 13px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px}
#show-btn:hover{background:#1e1b40;border-color:#7c3aed}
#show-btn.filtered{background:#1e1b40;color:#c4b5fd;border-color:#7c3aed;box-shadow:0 0 6px #7c3aed44}
/* Show Config Modal */
#show-modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;align-items:flex-start;justify-content:center;padding-top:60px}
#show-modal-overlay.open{display:flex}
#show-modal{background:#0d0d0d;border:1px solid #2d2d2d;border-radius:6px;padding:20px 24px;width:520px;max-width:90vw;max-height:80vh;overflow-y:auto;font-size:12px}
#show-modal h2{color:#c4b5fd;font-size:13px;letter-spacing:2px;text-transform:uppercase;margin:0 0 16px;font-weight:600}
.show-profiles{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.profile-btn{background:#111;color:#888;border:1px solid #2a2a2a;padding:5px 14px;border-radius:12px;font-family:inherit;font-size:11px;cursor:pointer;letter-spacing:.5px;transition:all .1s}
.profile-btn:hover{border-color:#7c3aed;color:#c4b5fd}
.profile-btn.active{background:#1e1b40;color:#c4b5fd;border-color:#7c3aed}
.genre-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:16px}
.genre-chk{display:flex;align-items:center;gap:6px;cursor:pointer;color:#888;font-size:11px;padding:4px 6px;border-radius:3px}
.genre-chk:hover{background:#1a1a1a;color:#ccc}
.genre-chk input{accent-color:#7c3aed;cursor:pointer}
.genre-chk.checked{color:#ccc}
#show-apply{background:#3b2d6e;color:#c4b5fd;border:1px solid #7c3aed;padding:7px 20px;border-radius:3px;font-family:inherit;font-size:12px;cursor:pointer;font-weight:bold;letter-spacing:1px;width:100%}
#show-apply:hover{background:#4c1d95}
/* Setlist modal */
#setlist-btn{background:#1a1a1a;color:#6ee7b7;border:1px solid #065f46;padding:5px 13px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px}
#setlist-btn:hover{background:#064e3b;border-color:#34d399}
#setlist-btn.has-tracks{background:#064e3b;color:#6ee7b7;border-color:#34d399;box-shadow:0 0 6px #34d39944}
#reset-btn{background:#1a1a1a;color:#f87171;border:1px solid #7f1d1d;padding:5px 13px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;letter-spacing:1px}
#reset-btn:hover{background:#450a0a;border-color:#ef4444}
#setlist-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;align-items:flex-start;justify-content:center;padding-top:60px}
#setlist-overlay.open{display:flex}
#setlist-modal{background:#0d0d0d;border:1px solid #2d2d2d;border-radius:6px;padding:20px 24px;width:560px;max-width:90vw;max-height:80vh;overflow-y:auto;font-size:12px}
#setlist-modal h2{color:#6ee7b7;font-size:13px;letter-spacing:2px;text-transform:uppercase;margin:0 0 4px;font-weight:600}
#setlist-subtitle{color:#555;font-size:11px;margin-bottom:16px}
#setlist-list{list-style:none;padding:0;margin:0 0 14px}
#setlist-list li{display:flex;align-items:baseline;gap:10px;padding:5px 0;border-bottom:1px solid #1a1a1a}
#setlist-list .sl-num{color:#444;min-width:22px;text-align:right;font-size:10px}
#setlist-list .sl-time{color:#555;font-size:10px;min-width:38px}
#setlist-list .sl-artist{color:#ccc;font-weight:600}
#setlist-list .sl-title{color:#888}
#setlist-list .sl-genre{color:#555;font-size:10px;margin-left:auto}
#setlist-empty{color:#555;font-style:italic;padding:20px 0;text-align:center}
.setlist-actions{display:flex;gap:8px}
#setlist-export-btn{flex:1;background:#064e3b;color:#6ee7b7;border:1px solid #065f46;padding:7px 14px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;font-weight:bold;letter-spacing:1px}
#setlist-export-btn:hover{background:#065f46}
#setlist-newwin-btn{background:#111;color:#888;border:1px solid #2a2a2a;padding:7px 14px;border-radius:3px;font-family:inherit;font-size:11px;cursor:pointer;letter-spacing:.5px}
#setlist-newwin-btn:hover{border-color:#888;color:#ccc}
/* ── Light mode overrides ── */
body.light #deck-bar{background:#ddd8d0;border-bottom-color:#b8b3ac}
body.light #osc-status.off{background:#d0ccc5;color:#777}
body.light .deck-pill{border-color:#b8b3ac;color:#666}
body.light .deck-pill.loaded{border-color:#888;color:#444;background:#d8d3cc}
body.light #deck-msg{color:#666}
body.light #swap-btn{background:#d8d3cc;color:#444;border-color:#b0ab a4}
body.light #swap-btn:hover{background:#c8c3bc;color:#222}
body.light #search-wrap{background:#e8e4de;border-bottom-color:#c8c3bc}
body.light #q{background:#f5f2ee;color:#222;border-color:#b8b3ac}
body.light #results{background:#ebe7e1;border-color:#b8b3ac}
body.light .r{border-bottom-color:#d8d3cc}.body.light .r:hover{background:#ddd8d0}
body.light .r .ra{color:#555}.body.light .r .rt{color:#111}
body.light #cols{background:#c8c3bc}
body.light .col{background:#ede9e3}
body.light .col-hdr{border-bottom-color:#d8d3cc}
body.light #c1 .col-hdr{color:#c0392b}
body.light #c2 .col-hdr{color:#d4650a}
body.light #c3 .col-hdr{color:#1a7fa0}
body.light .anchor-box{background:#f5ddd8;border-color:#c0392b}
body.light .anchor-box .an .aa{color:#c0392b}
body.light .anchor-box .an .at{color:#111}
body.light .tk{border-color:#d8d3cc;background:#ede9e3}
body.light .tk:hover{border-color:#999;background:#e0dbd4}
body.light .tk.sel{border-color:#d4650a;background:#fff0e0}
body.light .tk .ta{color:#444}.body.light .tk .tt{color:#111}
body.light .bpm{color:#b85a00}
body.light .key{color:#1a6080}
body.light .gen{color:#555}
body.light .scr{color:#2a7a3a}
body.light .lyric-summary{color:#777}
body.light .meta{color:#555}
body.light .bg-dest{color:#1a7fa0;border-left-color:#1a7fa0}
body.light .empty{color:#888}
body.light #rescue-box{background:#ede9e3;border-color:#c8c3bc}
body.light #activity-bar{background:#ddd8d0;border-bottom-color:#c8c3bc}
body.light .act-label{color:#b85a00}
body.light .act-track{color:#333}
body.light .act-bar-wrap{background:#c8c3bc}
body.light #show-modal{background:#ebe7e1;border-color:#c8c3bc}
body.light .profile-btn{background:#e2ddd7;color:#444;border-color:#c8c3bc}
body.light .genre-chk{color:#555}
body.light .genre-chk:hover{background:#d8d3cc;color:#222}
#rescue-box{display:none!important}
#rescue-box .r-label{font-size:9px;letter-spacing:2px;color:#666;margin-bottom:5px;text-transform:uppercase}
#rescue-box .r-track{font-size:13px;cursor:pointer}
#rescue-box .r-track .ra{color:#ccc}#rescue-box .r-track .rt{color:#fff;font-weight:bold}
#search-wrap{background:#161616;border-bottom:1px solid #222;padding:8px 18px;flex-shrink:0;position:relative}
#q{width:100%;background:#1e1e1e;color:#eee;border:1px solid #333;padding:8px 13px;font-size:14px;font-family:inherit;border-radius:3px}
#q:focus{outline:none;border-color:#e63946}
#results{position:absolute;left:18px;right:18px;background:#1a1a1a;border:1px solid #333;border-top:none;z-index:100;max-height:220px;overflow-y:auto;display:none}
.r{padding:8px 12px;cursor:pointer;border-bottom:1px solid #1f1f1f;display:flex;align-items:baseline;gap:10px}
.r:hover{background:#222}
.r .ra{color:#bbb}.r .rt{color:#fff;font-weight:bold}
#cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:#222;flex:1;overflow:hidden}
.col{background:#111;display:flex;flex-direction:column;overflow:hidden}
.col-hdr{padding:9px 14px;font-size:10px;letter-spacing:3px;text-transform:uppercase;border-bottom:1px solid #1f1f1f;flex-shrink:0}
#c1 .col-hdr{color:#e63946}#c2 .col-hdr{color:#f4a261}#c3 .col-hdr{color:#4cc9f0}
.col-body{overflow-y:auto;flex:1;padding:10px}
.anchor-box{background:#1a0808;border:1px solid #e63946;border-radius:4px;padding:12px}
.anchor-box .deck-tag{font-size:9px;color:#e63946;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px;opacity:0.7}
.anchor-box .an{font-size:14px;margin-bottom:5px}
.anchor-box .an .aa{color:#e63946}.anchor-box .an .at{color:#fff}
.tk{padding:9px 10px;margin-bottom:5px;border-radius:3px;cursor:pointer;border:1px solid #1e1e1e}
.tk:hover{border-color:#444;background:#181818}
.tk.sel{border-color:#f4a261;background:#1a1000}
.tk .tn{margin-bottom:4px}.tk .ta{color:#ccc}.tk .tt{color:#fff}
.meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;margin-top:3px}
.bpm{color:#f4a261}.key{color:#a8dadc}.gen{color:#aaa}.scr{color:#4a9}.sts{color:#ffd700;letter-spacing:-1px}
.rep-convicted{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#450a0a;color:#f87171;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.rep-accused{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#431407;color:#fb923c;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.rep-settled{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#052e16;color:#86efac;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.lyric-flag{display:inline-block;font-size:10px;padding:2px 7px;border-radius:3px;background:#2d1b4e;color:#c4b5fd;font-weight:bold;letter-spacing:1px;cursor:help;margin-left:4px}
.lyric-summary{font-size:11px;color:#a8adc8;margin-top:3px;white-space:normal;overflow-wrap:break-word;cursor:default}
body.light .lyric-summary{color:#666}
#lyr-tooltip{display:none;position:fixed;z-index:9999;pointer-events:none}
#lyr-tooltip .tk{zoom:2;min-width:220px;max-width:260px;cursor:default!important;border-color:#444!important;background:#181818!important;margin-bottom:0!important;box-shadow:0 8px 32px rgba(0,0,0,.8)}
body.light #lyr-tooltip .tk{background:#e8e3dd!important;border-color:#aaa!important}
.tx{font-size:10px;padding:2px 6px;border-radius:3px;font-weight:bold;letter-spacing:1px;text-transform:uppercase}
.tx-beat{background:#14532d;color:#4ade80}.tx-frag{background:#713f12;color:#facc15}
.tx-beatfx{background:#7c2d12;color:#fb923c}.tx-blend{background:#164e63;color:#a8dadc}
.tx-stem{background:#701a75;color:#e879f9}.tx-loop{background:#4a1d96;color:#c084fc}
.tx-efx{background:#7f1d1d;color:#f87171}.tx-cut{background:#1e293b;color:#94a3b8}
.bg{margin-bottom:12px}
.bg-dest{font-size:10px;color:#4cc9f0;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px;padding-left:6px;border-left:2px solid #4cc9f0}
.empty{color:#666;padding:16px;font-size:12px;text-align:center;line-height:1.8}
.deck-cards{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.dc{padding:8px 10px;border-radius:4px;cursor:pointer;border:1px solid #1e1e1e;background:#141414;transition:border-color .15s}
.dc:hover{border-color:#444;background:#1a1a1a}
.dc.dc-idle{border-color:#2a2a2a;color:#777}
.dc.dc-loaded{border-color:#444;background:#161616}
.dc.dc-playing{border-color:#e63946;background:#1a0808;box-shadow:0 0 8px #e6394633}
.dc .dc-label{font-size:9px;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px;color:#777}
.dc.dc-loaded .dc-label{color:#999}
.dc.dc-playing .dc-label{color:#e63946}
.dc .dc-name{font-size:12px;line-height:1.4}
.dc .dc-artist{color:#ccc}
.dc .dc-title{color:#fff}
.dc .dc-sep{color:#555}
.dc .dc-meta{font-size:10px;color:#888;margin-top:3px}
.dc.dc-loaded .dc-meta{color:#aaa}
.dc .dc-empty{color:#777;font-size:11px;font-style:italic}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#222;color:#ccc;border:1px solid #444;padding:6px 16px;border-radius:4px;font-size:11px;letter-spacing:1px;opacity:0;transition:opacity .15s;pointer-events:none;z-index:999}
#toast.show{opacity:1}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:#111}::-webkit-scrollbar-thumb{background:#2a2a2a}
#activity-bar{background:#0a0a0a;border-bottom:1px solid #1a1a1a;padding:5px 18px;display:none;align-items:center;gap:12px;flex-shrink:0;font-size:11px}
#activity-bar.active{display:flex}
#activity-bar .act-label{color:#4cc9f0;letter-spacing:1px;text-transform:uppercase;font-size:10px;white-space:nowrap;min-width:80px}
#activity-bar .act-track{flex:1;color:#888;white-space:normal;overflow-wrap:break-word;font-style:italic;font-size:10px}
#activity-bar .act-bar-wrap{width:160px;background:#1a1a1a;border-radius:2px;height:4px;overflow:hidden;flex-shrink:0}
#activity-bar .act-fill{background:#4cc9f0;height:100%;border-radius:2px;transition:width .4s}
#activity-bar .act-info{color:#555;white-space:nowrap;font-size:10px}
</style>
</head>
<body>
<div id="toast"></div>
<div id="lyr-tooltip"></div>
<div id="hdr">
  <h1>♪ DJ Block Planner</h1>
  <small id="tc">loading…</small>
  <span id="osc-status" class="off">OSC OFF</span>
  <button id="theme-btn" onclick="toggleTheme()" title="Switch between dark (indoor) and light (outdoor) mode">☀</button>
</div>
<div id="deck-bar">
  <span class="deck-pill" id="pill-a">DECK A</span>
  <span class="deck-pill" id="pill-b">DECK B</span>
  <button id="swap-btn" onclick="swapDecks()" title="Swap Deck A ↔ B assignments">⇄ Swap</button>
  <span id="deck-msg">Waiting for Traktor… or search below</span>
  <button class="panic-btn" id="save-btn" onclick="rescueMe('save')" title="Best rated floor track near current BPM/genre">🚨 Save Me</button>
  <button class="panic-btn" id="surprise-btn" onclick="rescueMe('surprise')" title="Highly rated track you haven't played tonight">✨ Surprise Me</button>
  <button id="show-btn" onclick="openShowConfig()" title="Configure show genre filter">🎛 Show Setup</button>
  <button id="setlist-btn" onclick="openSetlist()" title="View played tracks and export setlist">📋 Setlist</button>
  <button id="reset-btn" onclick="resetShow()" title="Clear played history for a fresh show">↺ Reset Show</button>
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
<div id="cols">
  <div class="col" id="c1">
    <div class="col-hdr">① Now Playing</div>
    <div class="col-body" id="b1"><div class="empty">Load a track in Traktor<br>— or search above.</div></div>
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

// ── Theme toggle (dark ↔ light) ───────────────────────────────────────────────
(function(){
  if(localStorage.getItem('theme')==='light'){
    document.body.classList.add('light');
    document.getElementById('theme-btn').textContent='🌙';
  }
})();
function toggleTheme(){
  const light=document.body.classList.toggle('light');
  document.getElementById('theme-btn').textContent=light?'🌙':'☀';
  localStorage.setItem('theme',light?'light':'dark');
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
  if(!t.transition)return'';
  return`<span class="tx tx-${TX_CLASS[t.transition]||'cut'}">${t.transition}</span>`;
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
function lyricLine(t){
  if(!t.lyric_summary)return'';
  return`<div class="lyric-summary">♪ ${esc(t.lyric_summary)}</div>`;
}
function tipCardHtml(t){
  const lyrHtml=t.lyric_summary
    ?`<div class="lyric-summary" style="white-space:normal;overflow-wrap:break-word">♪ ${esc(t.lyric_summary)}</div>`
    :'';
  return`<div class="tk"><div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${lyricBadges(t)}</div>${lyrHtml}${meta(t,true)}</div>`;
}
function meta(t,showScore){
  return`<div class="meta">
    <span class="bpm">${t.bpm} BPM</span><span class="key">${t.key||'—'}</span>
    <span class="gen">${t.genre||'—'}</span>${stars(t.stars)}
    ${showScore?`<span class="scr">${t.score}%</span>`:''}${txBadge(t)}</div>`;
}
function tkHtml(t,idx,sel,showScore){
  return`<div class="tk${sel?' sel':''}" id="s2-${idx}" data-track="${esc(JSON.stringify(t))}" onclick="pickSlot2(${idx});copyTrack('${esc(t.artist)}','${esc(t.title)}')">
    <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${lyricBadges(t)}</div>
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
  const label=`DECK ${deck.toUpperCase()}${playing?' ▶':''}`;
  const body=t
    ? `<div class="dc-name"><span class="dc-artist">${esc(t.artist)}</span><span class="dc-sep"> — </span><span class="dc-title">${esc(t.title)}</span></div>
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
}

let _rescueTrack=null;
async function rescueMe(mode){
  const anchorParam=anchor?'&anchor='+encodeURIComponent(anchor.path):'';
  const url=mode==='save'?`/api/save-me?${anchorParam.slice(1)}`:`/api/surprise-me`;
  const t=await fetch(url).then(r=>r.json());
  if(t.error){alert('No candidates found.');return}
  _rescueTrack=t;
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
function connectSSE(){
  const es=new EventSource('/api/events');
  es.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==='connected'){
      setOscOn();
      // Restore deck state after reconnect
      restoreDeckStatus();
      return;
    }
    if(d.type==='play_state'){deckPlayState(d.deck,d.playing);return}
    if(d.title||d.artist) deckLoaded(d.deck,d.title,d.artist,d.type==='playing');
  };
  es.onerror=()=>{
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
}

async function deckLoaded(deck,title,artist,isPlaying=false){
  const pill=deck==='a'?pillA:pillB;
  if(!pill.className.includes('playing')) pill.className='deck-pill loaded';
  deckMsg.textContent=`Deck ${deck.toUpperCase()} ${isPlaying?'▶':'→'} ${artist} — ${title}`;

  const r=await fetch(`/api/resolve-deck?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}`).then(r=>r.json());
  deckTracks[deck]=r||{artist,title,bpm:'?',key:'',genre:'',stars:0,path:''};
  renderDeckCards();

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

// ── Manual search ───────────────────────────────────────────────────────────
let st;
q.addEventListener('input',()=>{
  clearTimeout(st);
  if(q.value.trim().length<2){res.style.display='none';return}
  st=setTimeout(()=>doSearch(q.value.trim()),150);
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
  const deckTag=deck?`<div class="deck-tag">DECK ${deck.toUpperCase()} ▶ NOW PLAYING</div>`:'';
  // Replace anchor box only; keep deck cards
  const cardsEl=b1.querySelector('.deck-cards');
  const anchorHtml=`<div class="anchor-box" data-track="${esc(JSON.stringify(track))}">${deckTag}
    <div class="an"><span class="aa">${esc(track.artist)}</span><span style="color:#555"> — </span><span class="at">${esc(track.title)}</span></div>
    ${meta(track,false)}</div>`;
  if(cardsEl){
    const ab=b1.querySelector('.anchor-box');
    if(ab) ab.outerHTML=anchorHtml;
    else b1.insertAdjacentHTML('afterbegin',anchorHtml);
    renderDeckCards();
  } else {
    b1.innerHTML=anchorHtml;
    renderDeckCards();
  }
  b2.innerHTML='<div class="empty">Loading…</div>';
  b3.innerHTML='<div class="empty">Loading…</div>';
  const deckParam=deck?'&deck='+encodeURIComponent(deck):'';
  const d=await fetch('/api/suggest?path='+encodeURIComponent(track.path)+deckParam).then(r=>r.json());
  S2=d.slot2;
  renderSlot2(0);
  renderSlot3(d.slot3);
  if(S2.length)slot2=S2[0];
}

// ── Slot 2 ──────────────────────────────────────────────────────────────────
function renderSlot2(selIdx){
  if(!S2.length){b2.innerHTML='<div class="empty">No close matches found</div>';return}
  b2.innerHTML=S2.map((t,i)=>tkHtml(t,i,i===selIdx,true)).join('');
}
async function pickSlot2(i){
  slot2=S2[i];renderSlot2(i);
  b3.innerHTML='<div class="empty">Loading…</div>';
  const d=await fetch('/api/slot3?slot2='+encodeURIComponent(slot2.path)+'&anchor='+encodeURIComponent(anchor.path)).then(r=>r.json());
  renderSlot3(d);
}

// ── Slot 3 ──────────────────────────────────────────────────────────────────
function renderSlot3(groups){
  if(!groups.length){b3.innerHTML='<div class="empty">No bridge candidates</div>';return}
  b3.innerHTML=groups.map(g=>`
    <div class="bg"><div class="bg-dest">→ ${esc(g.destination)}</div>
    ${g.tracks.map(t=>`<div class="tk" data-track="${esc(JSON.stringify(t))}" onclick="copyTrack('${esc(t.artist)}','${esc(t.title)}')">
      <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#555"> — </span><span class="tt">${esc(t.title)}</span>${repBadge(t)}${lyricBadges(t)}</div>
      ${lyricLine(t)}${meta(t,true)}</div>`).join('')}</div>`).join('');
}
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
<div id="footer">localhost:7334/setlist · DJ Block Planner</div>
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
    from flask import Flask, Response, jsonify, request, stream_with_context

    app   = Flask(__name__)
    index = {t.path: t for t in tracks}

    @app.route("/")
    def ui():
        return Response(HTML, mimetype="text/html")

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

    return app


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    print("DJ Block Planner — loading collection…", end=" ", flush=True)
    tracks = load_tracks(TRAKTOR_NML)
    print(f"{len(tracks)} tracks ready.")

    osc_state = OSCState()
    osc_on    = start_osc_server(osc_state, OSC_PORT)

    if osc_on:
        print(f"  OSC listening on port {OSC_PORT} — Traktor auto-detect active.")
    else:
        print(f"  OSC unavailable (port {OSC_PORT} in use or python-osc missing).")
        print(f"  Running in manual search mode.")

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

    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=PORT,
                               debug=False, use_reloader=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    run_key_listener()   # blocks main thread; x / Ctrl+C exits


if __name__ == "__main__":
    main()
