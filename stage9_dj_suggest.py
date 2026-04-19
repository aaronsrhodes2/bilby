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

Add four OUT mappings (Type: Output, each one):

  Control: Track > Title     Deck: Deck A   OSC Address: /deck/a/title
  Control: Track > Artist    Deck: Deck A   OSC Address: /deck/a/artist
  Control: Track > Title     Deck: Deck B   OSC Address: /deck/b/title
  Control: Track > Artist    Deck: Deck B   OSC Address: /deck/b/artist

Save and close Preferences. Traktor will now broadcast track info here.
─────────────────────────────────────────────────────────────────────────────
"""

import json
import queue
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE            = Path(__file__).parent
TRAKTOR_NML     = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
SUGGESTIONS_DIR = BASE / "suggestions"
PORT            = 5001
OSC_PORT        = 9000

# ── Data model ────────────────────────────────────────────────────────────────

RANKING_TO_STARS = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}

@dataclass
class Track:
    path:   str
    artist: str
    title:  str
    bpm:    float
    key:    str
    genre:  str
    stars:  int

    @property
    def search_text(self) -> str:
        return f"{self.artist} {self.title}".lower()

    def to_dict(self, score: float = 0.0, transition: str = "") -> dict:
        return {
            "path":       self.path,
            "artist":     self.artist,
            "title":      self.title,
            "bpm":        round(self.bpm, 1),
            "key":        self.key,
            "genre":      self.genre,
            "stars":      self.stars,
            "score":      round(score * 100),
            "transition": transition,
        }


# ── NML loader ────────────────────────────────────────────────────────────────

def load_tracks(nml_path: Path) -> list[Track]:
    tree = ET.parse(nml_path)
    coll = tree.getroot().find("COLLECTION")
    tracks = []
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
        ranking = int(info.get("RANKING", 0))
        path = traktor_to_abs(
            loc.get("VOLUME", ""), loc.get("DIR", ""), loc.get("FILE", "")
        )
        tracks.append(Track(
            path   = path,
            artist = artist,
            title  = title,
            bpm    = bpm,
            key    = info.get("KEY",   ""),
            genre  = info.get("GENRE", ""),
            stars  = RANKING_TO_STARS.get(ranking, 0),
        ))
    return tracks


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


GENRE_NEIGHBORS: dict[str, list[str]] = {
    "Gothic Rock":      ["Post-Punk", "Darkwave", "New Wave", "Alternative Rock"],
    "Darkwave":         ["Gothic Rock", "Post-Punk", "Synthpop", "EBM", "Ambient"],
    "Post-Punk":        ["Gothic Rock", "Darkwave", "New Wave", "Alternative Rock", "Punk"],
    "EBM":              ["Industrial", "Synthpop", "Electronic", "Darkwave"],
    "Industrial":       ["EBM", "Electronic", "Metal", "Hard Rock"],
    "New Wave":         ["Synthpop", "Post-Punk", "Gothic Rock", "Pop"],
    "Synthpop":         ["New Wave", "EBM", "Darkwave", "Electronic", "Pop"],
    "Electronic":       ["EBM", "Industrial", "Ambient", "Synthpop"],
    "Ambient":          ["Electronic", "Darkwave", "Classical", "Soundtrack"],
    "Rock":             ["Alternative Rock", "Classic Rock", "Hard Rock", "Indie Rock"],
    "Alternative Rock": ["Rock", "Indie Rock", "Post-Punk", "Punk"],
    "Indie Rock":       ["Alternative Rock", "Rock", "Post-Punk", "Folk"],
    "Classic Rock":     ["Rock", "Hard Rock", "Folk"],
    "Hard Rock":        ["Rock", "Metal", "Classic Rock"],
    "Punk":             ["Post-Punk", "Alternative Rock", "Hard Rock"],
    "Metal":            ["Hard Rock", "Industrial", "Punk"],
    "Pop":              ["Synthpop", "New Wave", "Electronic", "Rock"],
    "Folk":             ["Indie Rock", "Alternative Rock", "Classic Rock"],
    "Soundtrack":       ["Ambient", "Electronic", "Classical"],
    "Hip-Hop":          ["Electronic", "Pop"],
    "Comedy":           ["Pop", "Other"],
    "Classical":        ["Ambient", "Soundtrack"],
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


# ── Block suggestions ─────────────────────────────────────────────────────────

def suggest_slot2(anchor: Track, tracks: list[Track], n: int = 8) -> list[dict]:
    results = []
    for t in tracks:
        if t.path == anchor.path: continue
        gf = 1.0 if t.genre == anchor.genre else genre_compat(anchor.genre, t.genre) * 0.5
        score = (
            0.35 * bpm_compat(anchor.bpm, t.bpm) +
            0.35 * key_compat(anchor.key, t.key) +
            0.20 * gf +
            0.10 * (t.stars / 5.0)
        )
        results.append((score, t))
    results.sort(key=lambda x: -x[0])
    return [t.to_dict(s, transition_type(anchor, t)) for s, t in results[:n] if s > 0.1]


def suggest_slot3(slot2: Track, anchor: Track, tracks: list[Track]) -> list[dict]:
    dest_genres = GENRE_NEIGHBORS.get(anchor.genre, [])
    if not dest_genres:
        all_genres  = list({t.genre for t in tracks if t.genre})
        dest_genres = [g for g in all_genres if g != anchor.genre][:8]
    exclude = {anchor.path, slot2.path}
    groups  = []
    for dest in dest_genres:
        candidates = []
        for t in tracks:
            if t.path in exclude: continue
            mix    = 0.5 * bpm_compat(slot2.bpm, t.bpm) + 0.5 * key_compat(slot2.key, t.key)
            bridge = 1.0 if t.genre == dest else (
                     0.5 if dest in GENRE_NEIGHBORS.get(t.genre, []) else 0.0)
            candidates.append((0.55 * mix + 0.45 * bridge, t))
        candidates.sort(key=lambda x: -x[0])
        top = [(s, t) for s, t in candidates[:3] if s > 0.25]
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


def print_suggestions(
    deck: str | None,
    anchor: Track,
    slot2: list[dict],
    slot3_groups: list[dict],
) -> None:
    """Clear terminal and print a fresh suggestion block."""
    # Clear screen + move to top
    print("\033[2J\033[H", end="", flush=True)

    # ── Header ───────────────────────────────────────────────────────────────
    deck_tag  = f"{_RED}DECK {deck.upper()} ▶{_R}  " if deck else "  "
    stars_str = _GLD + _STARS.get(anchor.stars, "     ") + _R
    bpm_str   = _YLW + f"{anchor.bpm:.1f}" + _R
    key_str   = _CYN + anchor.key + _R
    gre_str   = _GRY + anchor.genre + _R

    print(_HDR)
    print(f"  {deck_tag}{_WHT}{anchor.artist} — {anchor.title}{_R}")
    print(f"            {bpm_str} BPM  │  {key_str}  │  {gre_str}  │  {stars_str}")
    print(_HDR)

    # ── Slot 2 — Lock ────────────────────────────────────────────────────────
    print(f"\n  {_WHT}LOCK — PLAY NEXT{_R}\n")
    for t in slot2[:5]:
        print(_track_line(t))
        print()

    # ── Slot 3 — Bridge ──────────────────────────────────────────────────────
    if slot3_groups:
        print(f"  {_WHT}BRIDGE — AFTER THAT{_R}\n")
        for group in slot3_groups[:4]:
            print(f"  {_GRY}→ {group['destination']}{_R}")
            for t in group["tracks"][:2]:
                print(_track_line(t, show_tx=True))
                print()

    print(_SEP, flush=True)


# ── OSC state ─────────────────────────────────────────────────────────────────

class OSCState:
    """Thread-safe buffer for Traktor OSC deck events."""
    def __init__(self):
        self._lock     = threading.Lock()
        self._pending  = {}   # deck → {title, artist}
        self._sse_qs   = []   # SSE client queues

    def on_message(self, deck: str, field: str, value: str):
        with self._lock:
            p = self._pending.setdefault(deck, {})
            p[field] = value.strip()
            if "title" in p and "artist" in p:
                event = {"deck": deck, "title": p["title"], "artist": p["artist"]}
                for q in list(self._sse_qs):
                    try: q.put_nowait(event)
                    except: pass
                self._pending[deck] = {}   # reset after firing

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
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#111;color:#ddd;font-family:'Courier New',monospace;font-size:13px;height:100vh;display:flex;flex-direction:column}
#hdr{background:#0d0d1a;padding:10px 18px;border-bottom:2px solid #e63946;display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{color:#e63946;font-size:15px;letter-spacing:3px;text-transform:uppercase;flex:1}
#hdr small{color:#444;font-size:11px}
#osc-status{font-size:10px;padding:3px 8px;border-radius:3px;letter-spacing:1px;text-transform:uppercase}
#osc-status.on{background:#14532d;color:#4ade80}
#osc-status.off{background:#1e293b;color:#555}
#deck-bar{background:#0a0a0a;border-bottom:1px solid #1a1a1a;padding:6px 18px;display:flex;gap:12px;align-items:center;flex-shrink:0;min-height:34px}
.deck-pill{font-size:10px;padding:3px 10px;border-radius:12px;letter-spacing:1px;text-transform:uppercase;border:1px solid #222;color:#444}
.deck-pill.live{border-color:#e63946;color:#e63946;background:#1a0808}
#deck-msg{color:#555;font-size:11px;flex:1}
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
.tk .tn{margin-bottom:4px}.tk .ta{color:#999}.tk .tt{color:#fff}
.meta{display:flex;gap:8px;flex-wrap:wrap;font-size:11px;margin-top:3px}
.bpm{color:#f4a261}.key{color:#a8dadc}.gen{color:#666}.scr{color:#4a9}.sts{color:#ffd700;letter-spacing:-1px}
.tx{font-size:10px;padding:2px 6px;border-radius:3px;font-weight:bold;letter-spacing:1px;text-transform:uppercase}
.tx-beat{background:#14532d;color:#4ade80}.tx-frag{background:#713f12;color:#facc15}
.tx-beatfx{background:#7c2d12;color:#fb923c}.tx-blend{background:#164e63;color:#a8dadc}
.tx-stem{background:#701a75;color:#e879f9}.tx-loop{background:#4a1d96;color:#c084fc}
.tx-efx{background:#7f1d1d;color:#f87171}.tx-cut{background:#1e293b;color:#94a3b8}
.bg{margin-bottom:12px}
.bg-dest{font-size:10px;color:#4cc9f0;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px;padding-left:6px;border-left:2px solid #4cc9f0}
.empty{color:#333;padding:16px;font-size:12px;text-align:center;line-height:1.8}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:#111}::-webkit-scrollbar-thumb{background:#2a2a2a}
</style>
</head>
<body>
<div id="hdr">
  <h1>♪ DJ Block Planner</h1>
  <small id="tc">loading…</small>
  <span id="osc-status" class="off">OSC OFF</span>
</div>
<div id="deck-bar">
  <span class="deck-pill" id="pill-a">DECK A</span>
  <span class="deck-pill" id="pill-b">DECK B</span>
  <span id="deck-msg">Waiting for Traktor… or search below</span>
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
  document.getElementById('tc').textContent=d.count+' tracks · offline';
  if(d.osc) setOscOn();
});

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
function meta(t,showScore){
  return`<div class="meta">
    <span class="bpm">${t.bpm} BPM</span><span class="key">${t.key||'—'}</span>
    <span class="gen">${t.genre||'—'}</span>${stars(t.stars)}
    ${showScore?`<span class="scr">${t.score}%</span>`:''}${txBadge(t)}</div>`;
}
function tkHtml(t,idx,sel,showScore){
  return`<div class="tk${sel?' sel':''}" id="s2-${idx}" onclick="pickSlot2(${idx})">
    <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#333"> — </span><span class="tt">${esc(t.title)}</span></div>
    ${meta(t,showScore)}</div>`;
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

// ── SSE — auto-detect from Traktor ─────────────────────────────────────────
function connectSSE(){
  const es=new EventSource('/api/events');
  es.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==='connected'){setOscOn();return}
    if(d.title||d.artist) deckLoaded(d.deck,d.title,d.artist);
  };
  es.onerror=()=>{setTimeout(connectSSE,3000)};
}
connectSSE();

async function deckLoaded(deck,title,artist){
  // Highlight the active deck pill
  pillA.className='deck-pill'+(deck==='a'?' live':'');
  pillB.className='deck-pill'+(deck==='b'?' live':'');
  deckMsg.textContent=`Deck ${deck.toUpperCase()} loaded: ${artist} — ${title}`;

  const r=await fetch(`/api/resolve-deck?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}`).then(r=>r.json());
  if(r){
    SR=[r]; await loadAnchor(r,deck);
  } else {
    b1.innerHTML=`<div class="empty" style="color:#666">Deck ${deck.toUpperCase()}: <b style="color:#aaa">${esc(artist)} — ${esc(title)}</b><br><span style="color:#444">Not in collection</span></div>`;
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
    <div class="r" onclick="setAnchor(${i})">
      <span style="flex:1"><span class="ra">${esc(t.artist)}</span><span style="color:#444"> — </span><span style="color:#fff">${esc(t.title)}</span></span>
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
  b1.innerHTML=`<div class="anchor-box">${deckTag}
    <div class="an"><span class="aa">${esc(track.artist)}</span><span style="color:#555"> — </span><span class="at">${esc(track.title)}</span></div>
    ${meta(track,false)}</div>`;
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
    ${g.tracks.map(t=>`<div class="tk">
      <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#333"> — </span><span class="tt">${esc(t.title)}</span></div>
      ${meta(t,true)}</div>`).join('')}</div>`).join('');
}
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

    @app.route("/api/count")
    def count():
        return jsonify({"count": len(tracks), "osc": osc_on})

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
    print(f"\n  Open in browser:  http://localhost:{PORT}")
    print(f"  Playlists:        {SUGGESTIONS_DIR}/deck_a.m3u  |  deck_b.m3u")
    print(f"  (pin the suggestions/ folder in Traktor Explorer — navigate back to refresh)")
    print(f"  Stop:             Ctrl+C\n")

    app = make_app(tracks, osc_state, osc_on)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False,
            threaded=True)


if __name__ == "__main__":
    main()
