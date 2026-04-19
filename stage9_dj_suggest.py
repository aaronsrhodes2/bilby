#!/usr/bin/env python3
"""
Stage 9 — DJ Block Planner

3-track block suggester for live DJing. Fully offline. Zero CPU impact on Traktor.

  Slot 1 — Anchor : you pick it (popular crowd-pleaser, sets genre/tempo)
  Slot 2 — Lock   : tight BPM + key match, same genre
  Slot 3 — Bridge : still mixes from Slot 2, leads toward next genre block

Run:   python3 stage9_dj_suggest.py
Open:  http://localhost:5001
"""

import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.nml_parser import traktor_to_abs

# ── Paths ─────────────────────────────────────────────────────────────────────

BASE        = Path(__file__).parent
TRAKTOR_NML = Path.home() / "Documents/Native Instruments/Traktor 4.0.2/collection.nml"
PORT        = 5001

# ── Data model ────────────────────────────────────────────────────────────────

RANKING_TO_STARS = {0: 0, 51: 1, 102: 2, 153: 3, 204: 4, 255: 5}

@dataclass
class Track:
    path:   str
    artist: str
    title:  str
    bpm:    float
    key:    str    # Open Key: "12m", "1d", …
    genre:  str
    stars:  int    # 0–5

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
    """Open Key (Traktor) harmonic compatibility — 0.0 to 1.0."""
    if not k1 or not k2:
        return 0.5
    try:
        m1, m2 = k1[-1], k2[-1]
        n1, n2 = int(k1[:-1]), int(k2[:-1])
    except (ValueError, IndexError):
        return 0.5
    if n1 == n2 and m1 == m2: return 1.0   # perfect
    if n1 == n2:               return 0.9   # relative major/minor
    diff = min(abs(n1 - n2), 12 - abs(n1 - n2))
    if diff == 1 and m1 == m2: return 0.85  # adjacent same mode
    if diff == 1:              return 0.6   # adjacent diff mode
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

# Labels, CSS class suffix, hex colour
TRANSITIONS = {
    "BEAT MATCH":    ("BEAT MATCH",    "beat",    "#4ade80"),  # green  — tight beat mix
    "FRAGMENT":      ("BEAT+FRAGMENT", "frag",    "#facc15"),  # yellow — use 3rd song fragment
    "BEAT+FX":       ("BEAT+FX",       "beatfx",  "#fb923c"),  # orange — beat mix + effect cover
    "STEM BLEND":    ("STEM BLEND",    "stem",    "#e879f9"),  # pink   — use Traktor stems for vocal/drum control
    "BLEND":         ("BLEND",         "blend",   "#a8dadc"),  # teal   — slow crossfade
    "LOOP DROP":     ("LOOP DROP",     "loop",    "#c084fc"),  # purple — loop vocal, then release
    "EFFECT FADE":   ("EFFECT FADE",   "efx",     "#f87171"),  # red    — FX in/out, hide mismatch
    "CUT":           ("CUT",           "cut",     "#94a3b8"),  # grey   — hard cut
}

def transition_type(src: Track, dst: Track) -> str:
    """
    Infer the best transition technique from src → dst based on
    BPM proximity, key compatibility, and genre match.
    """
    bpm_d = abs(src.bpm - dst.bpm) if src.bpm > 0 and dst.bpm > 0 else 999
    kc    = key_compat(src.key, dst.key)
    same  = src.genre == dst.genre

    # Within 2.5 BPM, same genre, good key → can use fragment of a 3rd track
    if bpm_d <= 2.5 and same and kc >= 0.8:
        return "FRAGMENT"
    # Within 6 BPM, compatible key → clean beat mix
    if bpm_d <= 6 and kc >= 0.8:
        return "BEAT MATCH"
    # Within 6 BPM but key clash → beat mix needs effect cover
    if bpm_d <= 6 and kc < 0.4:
        return "BEAT+FX"
    # Within 6 BPM, moderate key → beat mix with light touch
    if bpm_d <= 6:
        return "BEAT MATCH"
    # Within 12 BPM, decent key, different genre → stem blend (cross-genre benefits most)
    if bpm_d <= 12 and kc >= 0.5 and not same:
        return "STEM BLEND"
    # Within 12 BPM, same genre → regular blend
    if bpm_d <= 12 and kc >= 0.5:
        return "BLEND"
    # Key is totally incompatible → hide it with effects
    if kc < 0.25:
        return "EFFECT FADE"
    # Big BPM gap but key is OK → loop the incoming vocal, drift in
    if bpm_d > 12 and kc >= 0.6:
        return "LOOP DROP"
    # Everything else → blend or cut depending on energy
    return "BLEND"


# ── Block suggestions ─────────────────────────────────────────────────────────

def suggest_slot2(anchor: Track, tracks: list[Track], n: int = 8) -> list[dict]:
    results = []
    for t in tracks:
        if t.path == anchor.path:
            continue
        gf = 1.0 if t.genre == anchor.genre else genre_compat(anchor.genre, t.genre) * 0.5
        score = (
            0.35 * bpm_compat(anchor.bpm,  t.bpm) +
            0.35 * key_compat(anchor.key,  t.key) +
            0.20 * gf +
            0.10 * (t.stars / 5.0)
        )
        results.append((score, t))
    results.sort(key=lambda x: -x[0])
    return [t.to_dict(s, transition_type(anchor, t)) for s, t in results[:n] if s > 0.1]


def suggest_slot3(slot2: Track, anchor: Track, tracks: list[Track]) -> list[dict]:
    """
    For each destination genre (neighbors of anchor's genre), find the
    best bridge candidates that still mix from slot2.
    Returns list of {destination, tracks[]} sorted by best score.
    """
    dest_genres = GENRE_NEIGHBORS.get(anchor.genre, [])
    if not dest_genres:
        all_genres = list({t.genre for t in tracks if t.genre})
        dest_genres = [g for g in all_genres if g != anchor.genre][:8]

    exclude = {anchor.path, slot2.path}
    groups = []

    for dest in dest_genres:
        candidates = []
        for t in tracks:
            if t.path in exclude:
                continue
            mix    = 0.5 * bpm_compat(slot2.bpm, t.bpm) + 0.5 * key_compat(slot2.key, t.key)
            if t.genre == dest:
                bridge = 1.0
            elif dest in GENRE_NEIGHBORS.get(t.genre, []):
                bridge = 0.5
            else:
                bridge = 0.0
            score = 0.55 * mix + 0.45 * bridge
            candidates.append((score, t))

        candidates.sort(key=lambda x: -x[0])
        top = [(s, t) for s, t in candidates[:3] if s > 0.25]
        if top:
            groups.append({
                "destination": dest,
                "tracks":      [t.to_dict(s) for s, t in top],
            })

    groups.sort(key=lambda g: -g["tracks"][0]["score"])
    return groups


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
#hdr{background:#0d0d1a;padding:12px 18px;border-bottom:2px solid #e63946;display:flex;align-items:center;gap:16px;flex-shrink:0}
#hdr h1{color:#e63946;font-size:16px;letter-spacing:3px;text-transform:uppercase}
#hdr small{color:#444;font-size:11px}
#search-wrap{background:#161616;border-bottom:1px solid #222;padding:10px 18px;flex-shrink:0;position:relative}
#q{width:100%;background:#1e1e1e;color:#eee;border:1px solid #333;padding:9px 13px;font-size:14px;font-family:inherit;border-radius:3px}
#q:focus{outline:none;border-color:#e63946}
#results{position:absolute;left:18px;right:18px;background:#1a1a1a;border:1px solid #333;border-top:none;z-index:100;max-height:240px;overflow-y:auto;display:none}
.r{padding:8px 12px;cursor:pointer;border-bottom:1px solid #1f1f1f;display:flex;align-items:baseline;gap:10px}
.r:hover{background:#222}
.r .ra{color:#bbb}.r .rt{color:#fff;font-weight:bold}
#cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:1px;background:#222;flex:1;overflow:hidden}
.col{background:#111;display:flex;flex-direction:column;overflow:hidden}
.col-hdr{padding:10px 14px;font-size:10px;letter-spacing:3px;text-transform:uppercase;border-bottom:1px solid #1f1f1f;flex-shrink:0}
#c1 .col-hdr{color:#e63946}
#c2 .col-hdr{color:#f4a261}
#c3 .col-hdr{color:#4cc9f0}
.col-body{overflow-y:auto;flex:1;padding:10px}
.anchor-box{background:#1a0808;border:1px solid #e63946;border-radius:4px;padding:12px}
.anchor-box .an{font-size:15px;margin-bottom:6px}
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
.tx-stem{background:#701a75;color:#e879f9}.tx-loop{background:#4a1d96;color:#c084fc}.tx-efx{background:#7f1d1d;color:#f87171}
.tx-cut{background:#1e293b;color:#94a3b8}
.bg{margin-bottom:12px}
.bg-dest{font-size:10px;color:#4cc9f0;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px;padding-left:6px;border-left:2px solid #4cc9f0}
.empty{color:#333;padding:16px;font-size:12px;text-align:center}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:#111}::-webkit-scrollbar-thumb{background:#2a2a2a}
</style>
</head>
<body>
<div id="hdr">
  <h1>♪ DJ Block Planner</h1>
  <small id="tc">loading…</small>
</div>
<div id="search-wrap">
  <input id="q" type="text" placeholder="Search artist or title…" autocomplete="off" spellcheck="false">
  <div id="results"></div>
</div>
<div id="cols">
  <div class="col" id="c1">
    <div class="col-hdr">① Anchor — Crowd Pleaser</div>
    <div class="col-body" id="b1"><div class="empty">Search above and click a track<br>to start building your block.</div></div>
  </div>
  <div class="col" id="c2">
    <div class="col-hdr">② Lock — Tight Mix</div>
    <div class="col-body" id="b2"><div class="empty">Pick an anchor first</div></div>
  </div>
  <div class="col" id="c3">
    <div class="col-hdr">③ Bridge — Lead Out</div>
    <div class="col-body" id="b3"><div class="empty">Pick an anchor first</div></div>
  </div>
</div>
<script>
let SR=[],S2=[],anchor=null,slot2=null;
const q=document.getElementById('q'),
      res=document.getElementById('results'),
      b1=document.getElementById('b1'),
      b2=document.getElementById('b2'),
      b3=document.getElementById('b3');

fetch('/api/count').then(r=>r.json()).then(d=>{document.getElementById('tc').textContent=d.count+' tracks · fully offline'});

function stars(n){
  return '<span class="sts">'+'★'.repeat(n)+'<span style="color:#2a2a2a">'+'·'.repeat(5-n)+'</span></span>';
}
const TX_CLASS = {
  'BEAT MATCH':'beat','BEAT+FRAGMENT':'frag','BEAT+FX':'beatfx',
  'STEM BLEND':'stem','BLEND':'blend','LOOP DROP':'loop','EFFECT FADE':'efx','CUT':'cut'
};
function txBadge(t){
  if(!t.transition) return '';
  const cls = TX_CLASS[t.transition]||'cut';
  return `<span class="tx tx-${cls}">${t.transition}</span>`;
}
function meta(t,showScore){
  return `<div class="meta">
    <span class="bpm">${t.bpm} BPM</span>
    <span class="key">${t.key||'—'}</span>
    <span class="gen">${t.genre||'—'}</span>
    ${stars(t.stars)}
    ${showScore?`<span class="scr">${t.score}%</span>`:''}
    ${txBadge(t)}
  </div>`;
}
function tkHtml(t,idx,sel,showScore){
  return `<div class="tk${sel?' sel':''}" id="s2-${idx}" onclick="pickSlot2(${idx})">
    <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#333"> — </span><span class="tt">${esc(t.title)}</span></div>
    ${meta(t,showScore)}
  </div>`;
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

// ── Search ─────────────────────────────────────────────────────────────────
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
      <span class="bpm">${t.bpm}</span>
      <span class="key">${t.key||'—'}</span>
      <span class="gen" style="min-width:100px">${t.genre||'—'}</span>
      ${stars(t.stars)}
    </div>`).join('');
  res.style.display='block';
}

// ── Set anchor ─────────────────────────────────────────────────────────────
async function setAnchor(i){
  anchor=SR[i]; res.style.display='none'; q.value='';
  b1.innerHTML=`<div class="anchor-box">
    <div class="an"><span class="aa">${esc(anchor.artist)}</span><span style="color:#555"> — </span><span class="at">${esc(anchor.title)}</span></div>
    ${meta(anchor,false)}
  </div>`;
  b2.innerHTML='<div class="empty">Loading…</div>';
  b3.innerHTML='<div class="empty">Loading…</div>';
  const d=await fetch('/api/suggest?path='+encodeURIComponent(anchor.path)).then(r=>r.json());
  S2=d.slot2;
  renderSlot2(0);
  renderSlot3(d.slot3);
  if(S2.length) slot2=S2[0];
}

// ── Slot 2 ─────────────────────────────────────────────────────────────────
function renderSlot2(selIdx){
  if(!S2.length){b2.innerHTML='<div class="empty">No close matches found</div>';return}
  b2.innerHTML=S2.map((t,i)=>tkHtml(t,i,i===selIdx,true)).join('');
}
async function pickSlot2(i){
  slot2=S2[i];
  renderSlot2(i);
  b3.innerHTML='<div class="empty">Loading…</div>';
  const d=await fetch('/api/slot3?slot2='+encodeURIComponent(slot2.path)+'&anchor='+encodeURIComponent(anchor.path)).then(r=>r.json());
  renderSlot3(d);
}

// ── Slot 3 ─────────────────────────────────────────────────────────────────
function renderSlot3(groups){
  if(!groups.length){b3.innerHTML='<div class="empty">No bridge candidates found</div>';return}
  b3.innerHTML=groups.map(g=>`
    <div class="bg">
      <div class="bg-dest">→ ${esc(g.destination)}</div>
      ${g.tracks.map(t=>`
        <div class="tk">
          <div class="tn"><span class="ta">${esc(t.artist)}</span><span style="color:#333"> — </span><span class="tt">${esc(t.title)}</span></div>
          ${meta(t,true)}
        </div>`).join('')}
    </div>`).join('');
}
</script>
</body>
</html>"""


def make_app(tracks: list[Track]):
    from flask import Flask, jsonify, request

    app   = Flask(__name__)
    index = {t.path: t for t in tracks}

    @app.route("/")
    def ui():
        from flask import Response
        return Response(HTML, mimetype="text/html")

    @app.route("/api/count")
    def count():
        return jsonify({"count": len(tracks)})

    @app.route("/api/search")
    def search():
        q = request.args.get("q", "").lower().strip()
        if len(q) < 2:
            return jsonify([])
        out = [t.to_dict() for t in tracks if q in t.search_text][:30]
        return jsonify(out)

    @app.route("/api/suggest")
    def suggest():
        t = index.get(request.args.get("path", ""))
        if not t:
            return jsonify({"error": "not found"}), 404
        s2 = suggest_slot2(t, tracks)
        ref = index.get(s2[0]["path"]) if s2 else t
        s3 = suggest_slot3(ref, t, tracks)
        return jsonify({"anchor": t.to_dict(), "slot2": s2, "slot3": s3})

    @app.route("/api/slot3")
    def slot3():
        s2t = index.get(request.args.get("slot2",  ""))
        anc = index.get(request.args.get("anchor", ""))
        if not s2t or not anc:
            return jsonify([])
        return jsonify(suggest_slot3(s2t, anc, tracks))

    return app


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)  # silence Flask request logs

    print("DJ Block Planner — loading collection…", end=" ", flush=True)
    tracks = load_tracks(TRAKTOR_NML)
    print(f"{len(tracks)} tracks ready.\n")
    print(f"  Open in browser:  http://localhost:{PORT}")
    print(f"  Stop:             Ctrl+C\n")

    app = make_app(tracks)
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
