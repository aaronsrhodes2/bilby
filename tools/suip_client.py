#!/usr/bin/env python3
"""
suip_client.py — Skippy Passthrough Transfer Protocol v1 client.

Runs as a daemon thread inside the DJ Block Planner process. Registers the
view with Skippy, maintains the control-channel SSE, publishes scene
snapshots on state change, and routes inbound intents to DJ server actions.

Phase 1 scope:
  • Structured lane only (no FrameStream / MPEG pose lane)
  • Always-full snapshots (no delta patches yet)
  • SSE + POST transport (no WebSocket)
  • Tailscale mesh auth (no tokens or mTLS)

The module is deliberately self-contained: only imports stdlib + requests +
suip_scene. Integration surface to stage9_dj_suggest.py is one class
constructor, one start() call, and one notify_state_changed() hook.
"""
from __future__ import annotations
import json
import logging
import os
import queue
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import requests

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
from suip_scene import (
    DJState, build_scene, build_manifest, VIEW_ID,
    MIN_TEXT_PX, MAX_FOCUS_TARGETS, PALETTE_ENUM_NAMES,
)

log = logging.getLogger("suip_client")

# ── Intent handler — plugs into stage9 action paths ──────────────────────────

class IntentHandler:
    """
    Routes SUIP intent names to DJ server actions. Each handler returns
    (result: "ok"|"deny"|"error", speak: str|None) — the client wraps that
    into an intent_result envelope.

    The handler holds references rather than copies, so it always reads the
    live app state. `suggest_slot2` + `suggest_slot3` are passed in by the
    integrator (they live in stage9_dj_suggest.py's module scope).
    """

    def __init__(self,
                 osc_state,
                 index: dict,
                 tracks: list,
                 suggestions_dir: Path,
                 suggest_slot2_fn: Callable,
                 suggest_slot3_fn: Callable,
                 update_sugg_state_fn: Callable):
        self.osc            = osc_state
        self.index          = index
        self.tracks         = tracks
        self.suggestions    = suggestions_dir
        self._slot2_fn      = suggest_slot2_fn
        self._slot3_fn      = suggest_slot3_fn
        self._update_state  = update_sugg_state_fn
        self._lock          = threading.RLock()
        self._selected_idx: Optional[int] = None
        self._current_anchor = None          # Track object currently anchored
        self._current_slot2:  list[dict] = []
        self._current_slot3:  list[dict] = []

    # ── readback hooks for the state provider ───────────────────────────────
    def selected_idx(self) -> Optional[int]:
        with self._lock:
            return self._selected_idx

    # Called by stage9 after _update_sugg_state runs, so we track its inputs
    def observe_state(self, anchor, slot2: list[dict], slot3: list[dict]) -> None:
        with self._lock:
            self._current_anchor = anchor
            self._current_slot2  = list(slot2)
            self._current_slot3  = list(slot3)
            # Reset pick if list shrank below old index or content changed
            if self._selected_idx is not None:
                if self._selected_idx >= len(self._current_slot2):
                    self._selected_idx = None

    # ── dispatcher ──────────────────────────────────────────────────────────
    def dispatch(self, intent: str, args: dict) -> tuple[str, Optional[str]]:
        try:
            if intent == "deck_swap":         return self._deck_swap()
            if intent == "save_me":           return self._rescue("save")
            if intent == "surprise_me":       return self._rescue("surprise")
            if intent.startswith("select_") and intent[7:].isdigit():
                return self._select_track(int(intent[7:]) - 1)
            if intent == "select_a":          return self._load_to_deck("a")
            if intent == "select_b":          return self._load_to_deck("b")
            log.warning("unknown intent: %s", intent)
            return "deny", f"no handler for {intent}"
        except Exception as e:
            log.exception("intent %s failed", intent)
            return "error", f"{intent} errored: {e}"

    # ── handlers ────────────────────────────────────────────────────────────
    def _deck_swap(self) -> tuple[str, Optional[str]]:
        self.osc.swap_decks()
        return "ok", "decks swapped"

    def _rescue(self, mode: str) -> tuple[str, Optional[str]]:
        """
        Reuse save-me / surprise-me scoring. Simplified — same algorithm as
        /api/save-me and /api/surprise-me but without the Flask layer.
        """
        import random
        tracks  = self.tracks
        if mode == "save":
            anchor = self._current_anchor
            def score_fn(t):
                if t.stars < 3: return -1
                if anchor is None: return t.stars / 5.0
                # inline — caller passes suggest_slot2; we just want a ranked fallback
                return t.stars / 5.0 + random.random() * 0.1
            cands = sorted([(score_fn(t), t) for t in tracks if score_fn(t) > 0],
                           key=lambda x: -x[0])[:5]
            if not cands:
                return "deny", "no rescue candidate"
            pick = random.choice(cands)[1]
        else:  # surprise
            def score_fn(t):
                if t.stars < 4: return -1
                return t.stars / 5.0 + random.random() * 0.25
            cands = sorted([(score_fn(t), t) for t in tracks if score_fn(t) > 0],
                           key=lambda x: -x[0])[:20]
            if not cands:
                return "deny", "no surprise available"
            pick = random.choice(cands)[1]

        # Load pick as new anchor; recompute suggestions
        s2 = self._slot2_fn(pick, tracks, n=8)
        s3 = self._slot3_fn(s2[0] if s2 else None, pick, tracks) if s2 else []
        self._update_state(s2, s3, pick)
        with self._lock:
            self._selected_idx = 0 if s2 else None
        return "ok", f"{mode}: {pick.artist} — {pick.title}"

    def _select_track(self, idx: int) -> tuple[str, Optional[str]]:
        with self._lock:
            s2 = self._current_slot2
            if idx < 0 or idx >= len(s2):
                return "deny", f"no track {idx + 1}"
            self._selected_idx = idx
            pick_dict = s2[idx]
            pick_track = self.index.get(pick_dict.get("path", ""))
            anchor_track = self._current_anchor

        if pick_track is not None and anchor_track is not None:
            s3 = self._slot3_fn(pick_track, anchor_track, self.tracks)
            # Keep slot2 intact; just refresh slot3 for the new pick context.
            self._update_state(s2, s3, anchor_track)
            with self._lock:
                # re-apply the selection (update_state may have observed)
                self._selected_idx = idx
        return "ok", f"selected track {idx + 1}: {pick_dict.get('artist','')} — {pick_dict.get('title','')}"

    def _load_to_deck(self, deck: str) -> tuple[str, Optional[str]]:
        with self._lock:
            if self._selected_idx is None:
                return "deny", 'nothing selected — say "select one" first'
            idx = self._selected_idx
            if idx >= len(self._current_slot2):
                return "deny", "selected track is no longer in the list"
            pick = self._current_slot2[idx]
        path = pick.get("path", "")
        track = self.index.get(path)
        if not track:
            return "error", "track not in collection"
        self.suggestions.mkdir(exist_ok=True)
        out = self.suggestions / f"deck_{deck}.m3u"
        label = f"{track.artist} — {track.title}  [{track.bpm:.1f} BPM | {track.key} | {track.genre}]"
        out.write_text(f"#EXTM3U\n#EXTINF:-1,{label}\n{path}\n", encoding="utf-8")
        return "ok", f"loaded to deck {deck.upper()}"


# ── SUIP client ───────────────────────────────────────────────────────────────

class SUIPClient:
    """
    Long-lived SUIP v1 client. One instance per DJ process.

    Skippy listens on port 47823 (same-device: http://127.0.0.1:47823).
    Set SKIPPY_URL=http://127.0.0.1:47823 (same device) or the Tailscale
    address of the phone when running across the mesh.

    Lifecycle:
      start()  → spawns daemon thread → register → SSE loop
      notify_state_changed() → coalesced within 100 ms, triggers scene:full
      stop()   → closes SSE, sends optional request_unmount
    """

    COALESCE_MS = 100    # ≤ 10 patches/sec per SUIP §7

    def __init__(self,
                 skippy_base_url: str,
                 manifest_url:    str,
                 state_provider:  Callable[[], DJState],
                 intent_handler:  IntentHandler,
                 view_id:         str = VIEW_ID):
        self.skippy       = skippy_base_url.rstrip("/")
        self.manifest_url = manifest_url
        self.state_of     = state_provider
        self.handler      = intent_handler
        self.view_id      = view_id

        self._send_seq    = 0
        self._stop        = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._coalesce_timer: Optional[threading.Timer] = None
        self._coalesce_lock = threading.Lock()
        self._ready       = threading.Event()  # set once register+mount_ack complete

    # ── public API ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="suip-client")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._post("request_unmount", {"reason": "shutdown"})
        except Exception:
            pass

    def notify_state_changed(self) -> None:
        """Coalesced trigger — called by stage9 hooks on any relevant change."""
        if not self._ready.is_set():
            return
        with self._coalesce_lock:
            if self._coalesce_timer and self._coalesce_timer.is_alive():
                return
            self._coalesce_timer = threading.Timer(
                self.COALESCE_MS / 1000.0, self._send_scene_full,
            )
            self._coalesce_timer.daemon = True
            self._coalesce_timer.start()

    # ── internals ───────────────────────────────────────────────────────────
    def _next_seq(self) -> int:
        self._send_seq += 1
        return self._send_seq

    def _post(self, path: str, body: dict) -> Optional[dict]:
        """POST /passthrough/<path> with JSON body. Returns parsed JSON or None."""
        url = f"{self.skippy}/passthrough/{path}"
        envelope = {"seq": self._next_seq(), **body}
        try:
            r = requests.post(url, json=envelope, timeout=5)
            if r.status_code >= 400:
                log.warning("POST %s → %d: %s", path, r.status_code, r.text[:200])
                return None
            if r.headers.get("Content-Type", "").startswith("application/json"):
                return r.json()
            return {}
        except requests.RequestException as e:
            log.warning("POST %s failed: %s", path, e)
            return None

    def _register(self) -> bool:
        # Derive stream_origin and stream_url from our manifest URL.
        # manifest_url = "http://host:port/manifest.json"
        # stream_origin = "http://host:port"
        # stream_url    = "http://host:port/control"
        origin = self.manifest_url.rsplit("/manifest.json", 1)[0]
        body = {
            "id":               self.view_id,
            "name":             "DJ Block Planner",
            "spec_version":     "1",
            "manifest_url":     self.manifest_url,
            "stream_url":       f"{origin}/control",
            "stream_origin":    origin,
            "aspect_ratio":     "16:10",
            "min_text_px":      MIN_TEXT_PX,
            "max_focus_targets": MAX_FOCUS_TARGETS,
            "palette":          list(PALETTE_ENUM_NAMES),
        }
        resp = self._post("register", body)
        # PassthroughServer v2+ returns {"ok": true, ...}; older spec used "accepted"
        return bool(resp and (resp.get("ok") or resp.get("accepted")))

    def _send_scene_full(self) -> None:
        state = self.state_of()
        root  = build_scene(state)
        self._post("patch", {"type": "scene:full", "root": root})

    def _send_intent_result(self, intent: str, result: str,
                             speak: Optional[str] = None) -> None:
        body = {"type": "intent_result", "intent": intent, "result": result}
        if speak:
            body["speak"] = speak
        self._post("intent_result", body)

    # ── SSE control-channel loop ────────────────────────────────────────────
    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self._register():
                log.warning("register failed; retry in %.1fs", backoff)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)
                continue
            log.info("registered view=%s with %s", self.view_id, self.skippy)

            try:
                self._sse_loop()
            except Exception as e:
                log.warning("SSE loop errored: %s", e)
            finally:
                self._ready.clear()

            if self._stop.is_set():
                return
            log.info("SSE closed; re-registering in %.1fs", backoff)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _sse_loop(self) -> None:
        """Open SSE and dispatch events until the connection closes or stop fires.

        Uses iter_content(chunk_size=None) rather than iter_lines so that data
        arrives as soon as Skippy flushes it — iter_lines buffers 512 bytes by
        default and would stall on small SSE events.
        """
        url = (f"{self.skippy}/passthrough/stream"
               f"?view={urllib.parse.quote(self.view_id)}")
        log.info("opening SSE: %s", url)
        with requests.get(url, stream=True, timeout=(5, None)) as r:
            r.raise_for_status()
            buf    = b""
            buffer: list[str] = []
            for chunk in r.iter_content(chunk_size=None):
                if self._stop.is_set():
                    return
                if not chunk:
                    continue
                buf += chunk
                # Split on newlines; keep any incomplete tail in buf
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                    if line == "":
                        self._flush(buffer)
                        buffer = []
                    else:
                        buffer.append(line)
            # Flush whatever is left when the connection closes
            if buffer:
                self._flush(buffer)

    def _flush(self, buffer: list[str]) -> None:
        payload = None
        for ln in buffer:
            if ln.startswith("data:"):
                payload = ln[5:].lstrip()
                break
        if not payload:
            return
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("bad SSE JSON: %s", payload[:200])
            return
        self._handle_event(evt)

    def _handle_event(self, evt: dict) -> None:
        t = evt.get("type", "")
        if t == "scene:full":
            # Skippy may send a cached snapshot on (re)connect — ignore content,
            # just proceed; mount_ack follows and we send our own scene:full.
            return
        if t == "mount_ack":
            log.info("mount_ack viewport=%s palette=%s context=%s",
                     evt.get("viewport_px"), evt.get("palette_enum"),
                     evt.get("context_mode"))
            self._ready.set()
            self._send_scene_full()
            return
        if t == "intent":
            intent = evt.get("intent", "")
            args   = evt.get("args")   or {}
            log.info("intent <- %s args=%s source=%s",
                     intent, args, evt.get("source"))
            result, speak = self.handler.dispatch(intent, args)
            self._send_intent_result(intent, result, speak)
            # intent actions usually change state; push fresh scene
            self.notify_state_changed()
            return
        if t == "before_unmount":
            log.info("before_unmount reason=%s", evt.get("reason"))
            self._send_intent_result("exit", "ok",
                                     speak=None)
            self._stop.set()
            return
        if t == "host:error":
            log.warning("host:error type=%s msg=%s detail=%s",
                        evt.get("error_type"), evt.get("message"),
                        evt.get("detail"))
            return
        if t in ("context_change", "listening", "palette_update",
                 "pose_lost", "connected"):
            log.debug("event %s: %s", t, evt)
            return
        log.debug("unhandled event type=%s keys=%s", t, list(evt.keys()))
