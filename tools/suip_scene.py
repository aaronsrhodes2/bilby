#!/usr/bin/env python3
"""
suip_scene.py — pure scene-tree + manifest builders for the
Skippy Passthrough Transfer Protocol v1.

No I/O, no Flask, no threads — just functions. Testable standalone:

    python3 tools/suip_scene.py          # print sample scene JSON

The DJ Block Planner's SUIP client imports `build_scene(dj_state)` and
`build_manifest()` from this module.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Optional

# ── Palette enum names (per SUIP v1 §5.4) ─────────────────────────────────────
#   Scene tree colors AND the registration body palette both use these names.
#   SkippyView declares exactly these seven; scene nodes must only use names
#   from this list (the _text() guard enforces it).
#
#   Hex reference (for documentation only — not sent in the protocol):
#     black=#000000  white=#FFFFFF  green=#00FF00  amber=#FFCC00
#     cyan=#00CCFF   violet=#818cf8 red=#FF3344

PALETTE_ENUM_NAMES = [
    "black", "white", "cyan", "amber", "green", "violet", "red",
]

# ── DJ state container (what the client hands us for scene rendering) ────────

@dataclass
class DJState:
    # anchor: Track.to_dict() output, or None when no anchor is set
    anchor:            Optional[dict] = None
    # slot2: list of candidate dicts (each with `score`, `transition` added)
    slot2:             list[dict]     = field(default_factory=list)
    # slot3: list of {"destination": str, "tracks": [dict]}
    slot3:             list[dict]     = field(default_factory=list)
    # deck state from osc_state
    deck_loaded:       dict           = field(default_factory=dict)  # {"a": {"title","artist"}|None, "b": …}
    deck_playing:      dict           = field(default_factory=dict)  # {"a": bool, "b": bool}
    # server-maintained slot2 selection (set by `select N` intent)
    selected_slot2_idx: Optional[int] = None


# ── Manifest ──────────────────────────────────────────────────────────────────

VIEW_ID       = "dj.block_planner"   # dot notation per SkippyView
VIEW_NAME     = "DJ Block Planner"
VIEW_VERSION  = "2.0.0"
MIN_TEXT_PX   = 22
MAX_FOCUS_TARGETS = 7   # 2 deck cards + 5 slot-2 candidates = 7 total FocusTargets
SPEC_VERSION  = "1"

# Voice commands — matches the JS KEYWORD_CMDS grammar in stage9_dj_suggest.py.
# Literal select_N intents (1..8) because slot2 is capped at 8 candidates.
# intent_args capability is deferred (SUIP v1 minor addition).
VOICE_COMMANDS = [
    {"phrase": ["swap decks", "swap"],             "intent": "deck_swap"},
    {"phrase": ["save me", "rescue"],              "intent": "save_me"},
    {"phrase": ["surprise me"],                    "intent": "surprise_me"},
    # 5 pick slots (2 decks + 5 tracks = 7 total FocusTargets; cap is MAX_FOCUS_TARGETS)
    {"phrase": ["select 1", "select one",   "track 1"], "intent": "select_1"},
    {"phrase": ["select 2", "select two",   "track 2"], "intent": "select_2"},
    {"phrase": ["select 3", "select three", "track 3"], "intent": "select_3"},
    {"phrase": ["select 4", "select four",  "track 4"], "intent": "select_4"},
    {"phrase": ["select 5", "select five",  "track 5"], "intent": "select_5"},
    {"phrase": ["select a", "load a", "deck a", "to a"], "intent": "select_a"},
    {"phrase": ["select b", "load b", "deck b", "to b"], "intent": "select_b"},
]


def build_manifest() -> dict:
    """Returned at GET /manifest.json — fetched by Skippy after registration."""
    return {
        "name":             VIEW_NAME,
        "version":          VIEW_VERSION,
        "spec_version":     SPEC_VERSION,
        "entry_url":        "/",
        "aspect_ratio":     "16:10",
        "palette":          list(PALETTE_ENUM_NAMES),
        "min_text_px":      MIN_TEXT_PX,
        "max_focus_targets": MAX_FOCUS_TARGETS,
        "voice_commands":   list(VOICE_COMMANDS),
    }


# ── Scene tree helpers (minimal, typed) ──────────────────────────────────────
#   Each helper returns a dict matching SUIP v1 §5.1 node shapes.

def _text(text: str, *, color: str = "white", size_px: int = MIN_TEXT_PX,
          weight: str = "normal", align: str = "start",
          monospace: bool = True, size_justify: bool = False,
          node_id: str = "") -> dict:
    if color not in PALETTE_ENUM_NAMES:
        color = "white"
    if size_px < MIN_TEXT_PX:
        size_px = MIN_TEXT_PX
    return {
        "id": node_id or f"t_{abs(hash(text))%1000000}",
        "type": "Text",
        "props": {
            "text":         text,
            "color":        color,
            "size_px":      size_px,
            "weight":       weight,
            "align":        align,
            "monospace":    monospace,
            "size_justify": size_justify,
        },
    }


def _row(children: list[dict], *, node_id: str, gap: int = 8,
         padding: int = 0, main_axis: str = "start",
         cross_axis: str = "start") -> dict:
    return {
        "id":       node_id,
        "type":     "Row",
        "props":    {"gap": gap, "padding": padding,
                     "main_axis": main_axis, "cross_axis": cross_axis},
        "children": children,
    }


def _col(children: list[dict], *, node_id: str, gap: int = 6,
         padding: int = 0, main_axis: str = "start",
         cross_axis: str = "start") -> dict:
    return {
        "id":       node_id,
        "type":     "Column",
        "props":    {"gap": gap, "padding": padding,
                     "main_axis": main_axis, "cross_axis": cross_axis},
        "children": children,
    }


def _focus(focus_id: str, intent: str, child: dict, *, node_id: str) -> dict:
    return {
        "id":    node_id,
        "type":  "FocusTarget",
        "props": {"focus_id": focus_id, "intent": intent},
        "children": [child],
    }


# ── Sub-scene: Deck strip (top — world/external) ─────────────────────────────

def _deck_card(deck: str, loaded: Optional[dict], playing: bool) -> dict:
    """One DECK A / DECK B cell."""
    deck_label = f"DECK {deck.upper()}" + (" ▶" if playing else "")
    label = _text(deck_label, color="green", size_px=MIN_TEXT_PX,
                  node_id=f"deck_{deck}_label")

    if not loaded:
        body = _text("—", color="green", size_px=MIN_TEXT_PX,
                     node_id=f"deck_{deck}_empty")
        inner = _col([label, body], node_id=f"deck_{deck}_inner", gap=6)
    else:
        artist = _text(loaded.get("artist", ""),
                       color="green" if playing else "white",
                       size_px=MIN_TEXT_PX + 6,
                       node_id=f"deck_{deck}_artist")
        title = _text(loaded.get("title", ""),
                      color="white", size_px=MIN_TEXT_PX + 2,
                      node_id=f"deck_{deck}_title")
        inner = _col([label, artist, title],
                     node_id=f"deck_{deck}_inner", gap=4)

    return _focus(
        focus_id=f"deck {deck}",
        intent=f"select_{deck}",
        child=inner,
        node_id=f"deck_{deck}",
    )


def _deck_strip(state: DJState) -> dict:
    a = _deck_card("a", state.deck_loaded.get("a"),
                   state.deck_playing.get("a", False))
    b = _deck_card("b", state.deck_loaded.get("b"),
                   state.deck_playing.get("b", False))
    return _row([a, b], node_id="deck_strip", gap=16, main_axis="space_between")


# ── Sub-scene: Selected Song (the anchor) ────────────────────────────────────

def _badge_text(text: str, color: str, node_id: str) -> dict:
    """A bold inline pill — Text node with the badge color."""
    return _text(text, color=color, size_px=MIN_TEXT_PX - 2,
                 weight="bold", node_id=node_id)


def _selected_column(state: DJState) -> dict:
    header = _text("① SELECTED SONG", color="green", size_px=MIN_TEXT_PX,
                   weight="bold", node_id="anchor_hdr")

    if not state.anchor:
        return _col(
            [header,
             _text("No anchor loaded. Load a track in Traktor or pick from Play Next.",
                   color="green", size_px=MIN_TEXT_PX,
                   size_justify=True, node_id="anchor_empty")],
            node_id="c_selected", gap=8,
        )

    a = state.anchor
    artist = _text(a.get("artist", ""), color="green",
                   size_px=MIN_TEXT_PX + 14, node_id="anchor_artist")
    title  = _text(a.get("title", ""), color="white",
                   size_px=MIN_TEXT_PX + 14, node_id="anchor_title")

    meta_bits = [
        _text(f"{a.get('bpm','?')} BPM", color="amber",
              size_px=MIN_TEXT_PX, node_id="anchor_bpm"),
        _text(a.get("key") or "—", color="cyan",
              size_px=MIN_TEXT_PX, node_id="anchor_key"),
        _text(a.get("genre") or "—", color="green",
              size_px=MIN_TEXT_PX, node_id="anchor_genre"),
        _text("★" * int(a.get("stars", 0) or 0), color="amber",
              size_px=MIN_TEXT_PX, node_id="anchor_stars"),
    ]
    meta = _row(meta_bits, node_id="anchor_meta", gap=12)

    # Badges — instrumental + rep + lyric flags
    badges: list[dict] = []
    if a.get("is_instrumental"):
        badges.append(_badge_text("♬ INSTR", "violet", "anchor_badge_instr"))
    rep_tier = a.get("rep_tier")
    if rep_tier == "convicted":
        badges.append(_badge_text("🔴 CONVICTED", "red", "anchor_badge_rep"))
    elif rep_tier == "accused":
        badges.append(_badge_text("⚠ ACCUSED", "amber", "anchor_badge_rep"))
    elif rep_tier == "settled":
        badges.append(_badge_text("🟢 SETTLED", "green", "anchor_badge_rep"))
    if a.get("song_flag"):
        badges.append(_badge_text("⚠ THIS SONG", "amber", "anchor_badge_song"))
    for i, f in enumerate(a.get("lyric_flags") or []):
        badges.append(_badge_text(f"🚫 {f.upper()}", "violet",
                                   f"anchor_badge_lf_{i}"))

    summary = _text(
        a.get("lyric_summary") or "",
        color="cyan",
        size_px=MIN_TEXT_PX + 2,
        size_justify=True,
        node_id="anchor_summary",
    )

    children = [header, artist, title, meta]
    if badges:
        children.append(_row(badges, node_id="anchor_badges_row", gap=6))
    if a.get("lyric_summary"):
        children.append(summary)
    return _col(children, node_id="c_selected", gap=8)


# ── Sub-scene: Play Next (the 8 slot-2 candidates) ──────────────────────────

def _candidate_card(t: dict, idx: int, selected: bool) -> dict:
    num = _text(f"[{idx+1}]", color="green", weight="bold",
                size_px=MIN_TEXT_PX + 2, node_id=f"s2_{idx}_num")
    art = _text(t.get("artist", ""),
                color="green" if selected else "white",
                size_px=MIN_TEXT_PX + 2, node_id=f"s2_{idx}_artist")
    sep = _text("—", color="green",
                size_px=MIN_TEXT_PX, node_id=f"s2_{idx}_sep")
    tit = _text(t.get("title", ""),
                color="white", size_px=MIN_TEXT_PX + 2,
                node_id=f"s2_{idx}_title")
    title_row = _row([num, art, sep, tit],
                     node_id=f"s2_{idx}_title_row", gap=8)

    meta_bits = [
        _text(f"{t.get('bpm','?')} BPM", color="amber",
              size_px=MIN_TEXT_PX, node_id=f"s2_{idx}_bpm"),
        _text(t.get("key") or "—", color="cyan",
              size_px=MIN_TEXT_PX, node_id=f"s2_{idx}_key"),
        _text(t.get("genre") or "—", color="green",
              size_px=MIN_TEXT_PX, node_id=f"s2_{idx}_genre"),
        _text(f"{t.get('score', 0)}%", color="green", weight="bold",
              size_px=MIN_TEXT_PX, node_id=f"s2_{idx}_score"),
    ]
    if t.get("transition"):
        meta_bits.append(_text(t["transition"], color="amber",
                               size_px=MIN_TEXT_PX, weight="bold",
                               node_id=f"s2_{idx}_tx"))
    if t.get("is_instrumental"):
        meta_bits.append(_badge_text("♬", "violet",
                                     f"s2_{idx}_instr"))
    meta = _row(meta_bits, node_id=f"s2_{idx}_meta", gap=10)

    children = [title_row, meta]
    if t.get("lyric_summary"):
        children.append(_text(
            t["lyric_summary"], color="green",
            size_px=MIN_TEXT_PX, size_justify=True,
            node_id=f"s2_{idx}_summary"))

    card = _col(children, node_id=f"s2_{idx}_card",
                gap=4, padding=8)

    return _focus(
        focus_id=f"track {idx+1}",
        intent=f"select_{idx+1}",
        child=card,
        node_id=f"s2_{idx}",
    )


def _play_next_column(state: DJState) -> dict:
    header = _text("② PLAY NEXT — LOCK", color="green",
                   size_px=MIN_TEXT_PX, weight="bold",
                   node_id="playnext_hdr")
    children: list[dict] = [header]

    if not state.slot2:
        children.append(_text("Waiting for suggestions…",
                              color="green", size_px=MIN_TEXT_PX,
                              node_id="playnext_wait"))
    else:
        sel = state.selected_slot2_idx
        for i, t in enumerate(state.slot2[:5]):  # 5 tracks + 2 decks = 7 FocusTargets
            children.append(_candidate_card(t, i, selected=(i == sel)))

    return _col(children, node_id="c_playnext", gap=6)


# ── Sub-scene: After That (the slot-3 bridge groups) ─────────────────────────

def _bridge_column(state: DJState) -> dict:
    header = _text("③ AFTER THAT — BRIDGE", color="green",
                   size_px=MIN_TEXT_PX, weight="bold",
                   node_id="bridge_hdr")
    children: list[dict] = [header]

    if not state.slot3:
        children.append(_text("Waiting for bridge candidates…",
                              color="green", size_px=MIN_TEXT_PX,
                              node_id="bridge_wait"))
        return _col(children, node_id="c_bridge", gap=6)

    for gi, grp in enumerate(state.slot3[:4]):
        dest = grp.get("destination") or "—"
        children.append(_text(f"→ {dest.upper()}", color="cyan",
                              weight="bold", size_px=MIN_TEXT_PX,
                              node_id=f"s3_g{gi}_hdr"))
        for ti, t in enumerate(grp.get("tracks", [])[:2]):
            art = _text(t.get("artist", ""), color="white",
                        size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_artist")
            sep = _text("—", color="green",
                        size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_sep")
            tit = _text(t.get("title", ""), color="white",
                        size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_title")
            row = _row([art, sep, tit],
                       node_id=f"s3_g{gi}_{ti}_title_row", gap=8)

            meta_bits = [
                _text(f"{t.get('bpm','?')} BPM", color="amber",
                      size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_bpm"),
                _text(t.get("key") or "—", color="cyan",
                      size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_key"),
                _text(f"{t.get('score',0)}%", color="green",
                      size_px=MIN_TEXT_PX, node_id=f"s3_g{gi}_{ti}_score"),
            ]
            meta = _row(meta_bits, node_id=f"s3_g{gi}_{ti}_meta", gap=8)

            card_kids: list[dict] = [row, meta]
            if t.get("lyric_summary"):
                card_kids.append(_text(
                    t["lyric_summary"], color="green",
                    size_px=MIN_TEXT_PX, size_justify=True,
                    node_id=f"s3_g{gi}_{ti}_summary"))
            children.append(_col(card_kids,
                                 node_id=f"s3_g{gi}_{ti}_card",
                                 gap=3, padding=6))

    return _col(children, node_id="c_bridge", gap=6)


# ── Root ─────────────────────────────────────────────────────────────────────

def build_scene(state: DJState) -> dict:
    """Build a complete scene tree root from DJ state. Returns the root node
    suitable for sending as the body of a `scene:full` patch."""
    deck_strip = _deck_strip(state)
    main = _row(
        [
            _selected_column(state),
            _play_next_column(state),
            _bridge_column(state),
        ],
        node_id="main_row", gap=16, cross_axis="start",
    )
    root = _col(
        [deck_strip, main],
        node_id="root", padding=16, gap=12,
    )
    return root


# ── Sanity / demo ────────────────────────────────────────────────────────────

def _demo_state() -> DJState:
    return DJState(
        anchor={
            "path": "/fake/anchor.mp3",
            "artist": "Abney Park", "title": "Breathe",
            "bpm": 118, "key": "5m", "genre": "Gothic Rock",
            "stars": 4, "score": 100, "transition": "",
            "lyric_summary": "A song about catching one's breath in the midst of despair.",
            "lyric_flags": [],
            "is_instrumental": False,
        },
        slot2=[
            {"artist": "Switchblade Symphony", "title": "Bad Trash",
             "bpm": 120.0, "key": "5m", "genre": "Gothic Rock",
             "stars": 4, "score": 93, "transition": "BEAT MATCH",
             "lyric_summary": "A plea for beauty in a bleak world.",
             "is_instrumental": False},
            {"artist": "Bauhaus", "title": "All We Ever Wanted",
             "bpm": 116.2, "key": "1d", "genre": "Gothic Rock",
             "stars": 5, "score": 92, "transition": "BEAT+FRAGMENT",
             "lyric_summary": "Unfulfilled desires and disillusionment.",
             "is_instrumental": False},
        ],
        slot3=[
            {"destination": "Post-Punk", "tracks": [
                {"artist": "Killing Joke", "title": "Primitive",
                 "bpm": 111.0, "key": "5d", "genre": "Post-Punk",
                 "stars": 4, "score": 95, "transition": "BEAT MATCH",
                 "lyric_summary": "Raw tribal primitivism.",
                 "is_instrumental": False},
            ]},
        ],
        deck_loaded={
            "a": {"artist": "Abney Park", "title": "Breathe"},
            "b": None,
        },
        deck_playing={"a": True, "b": False},
        selected_slot2_idx=0,
    )


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "scene"
    if mode == "manifest":
        print(json.dumps(build_manifest(), indent=2))
    else:
        print(json.dumps(build_scene(_demo_state()), indent=2))
