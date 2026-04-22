#!/usr/bin/env python3
"""
dispatch.py — generate, commit, and push a PC dispatch file.

Snapshots current Mac-side state (NML HOTCUE counts, lyrics, autocue progress,
album art stats), fills the matching template, writes tools/DISPATCH_PC_*.md,
commits, and pushes. The PC watcher (tools/pc_dispatch_watcher.py) picks it up
within 60 seconds and runs it under `claude --dangerously-skip-permissions`.

Usage:
    ./tools/dispatch.sh <job>              # the most common form
    python3 tools/dispatch.py <job>        # equivalent

Jobs:
    stt         STT lyrics + instrumental tagging (long — ~12–18 h)
    autocue     librosa Cue 3 & Cue 4 placement (long — ~6–10 h)
    art         Album art fetch/force-retry/embed (~1–2 h)
    status      Read-only audit; no work performed
    all         Kick off stt + autocue + art in parallel

Options:
    --dry-run   Print the dispatch to stdout without writing or committing
    --no-push   Commit locally but skip the push (stage multiple dispatches)
    --notes "…" Append operator-provided notes to the dispatch body

Examples:
    ./tools/dispatch.sh stt
    ./tools/dispatch.sh status --notes "quick check before tonight's set"
    ./tools/dispatch.sh autocue --dry-run
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
REPO  = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"


# ── Baseline snapshot (auto-embedded in every dispatch) ───────────────────────
def compute_baseline() -> dict:
    """Snapshot current state; cheap to compute, runs on every dispatch."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    short   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    # Lyrics
    p = REPO / "state" / "lyrics_dedup.json"
    lyrics_total = lyrics_instr = 0
    if p.exists():
        try:
            d = json.loads(p.read_text())
            lyrics_total = len(d)
            lyrics_instr = sum(
                1 for v in d.values()
                if isinstance(v, dict) and (v.get("theme") or "").lower() == "instrumental"
            )
        except Exception:
            pass

    # NML HOTCUE distribution
    nml = REPO / "corrected_traktor" / "collection.nml"
    hotcue: dict[str, int] = {}
    track_count = 0
    if nml.exists():
        try:
            tree = ET.parse(nml)
            coll = tree.getroot().find("COLLECTION")
            entries = coll.findall("ENTRY") if coll is not None else []
            track_count = len(entries)
            for e in entries:
                for c in e.findall("CUE_V2"):
                    hc = c.get("HOTCUE", "-1")
                    hotcue[hc] = hotcue.get(hc, 0) + 1
        except Exception:
            pass

    # Album art
    ai = REPO / "state" / "album_art_index.json"
    art_found = art_nulls = 0
    if ai.exists():
        try:
            idx = json.loads(ai.read_text())
            art_found = sum(1 for v in idx.values() if v)
            art_nulls = sum(1 for v in idx.values() if v is None)
        except Exception:
            pass

    # Autocue progress
    ap = REPO / "state" / "autocue_progress.json"
    ap_done = 0
    if ap.exists():
        try:
            ap_done = len((json.loads(ap.read_text()) or {}).get("done", []))
        except Exception:
            pass

    # "Gap" = tracks in NML minus tracks with any lyrics entry
    gap = max(0, track_count - lyrics_total)

    return {
        "timestamp":    now_iso,
        "short":        short,
        "track_count":  track_count,
        "lyrics_total": lyrics_total,
        "lyrics_instr": lyrics_instr,
        "lyrics_gap":   gap,
        "hc2":          hotcue.get("2", 0),   # UI slot 3
        "hc3":          hotcue.get("3", 0),   # UI slot 4
        "hc_all":       hotcue,
        "art_found":    art_found,
        "art_nulls":    art_nulls,
        "ap_done":      ap_done,
    }


# ── Templates — {...} fields resolved from baseline ──────────────────────────
TEMPLATES: dict[str, str] = {
    "stt": """# PC Dispatch — STT Lyrics Pass

Auto-generated {timestamp}.

## Baseline at dispatch time

| Metric | Count |
|---|---:|
| NML tracks | {track_count:,} |
| lyrics_dedup entries | {lyrics_total:,} |
| theme=instrumental | {lyrics_instr:,} |
| estimated gap needing STT | ~{lyrics_gap:,} |

## Run this

You are Claude on the Skippy PC. Run the STT lyrics pass and push results.

1. `cd D:\\Aaron\\development\\music-collection && git pull`
2. `python stage9_stt.py --report` (audit the gap first)
3. `python stage9_stt.py --run` (resumable; 12–18 h on a 4070 Ti)
4. Commit `state/lyrics_dedup.json`, `state/lyrics_raw.json`, and any
   `state/stt*progress.json` every 2 hours or on clean exit; push to origin.
5. Report back:
   - Tracks processed this run
   - New `theme=instrumental` count (delta from baseline {lyrics_instr})
   - Any errors — capture last 50 lines of stderr

Permission: bypass mode. Safe to interrupt and restart; progress file persists.
""",

    "autocue": """# PC Dispatch — Autocue Slots 3 & 4 (librosa)

Auto-generated {timestamp}.

## Baseline at dispatch time

| Metric | Count |
|---|---:|
| NML tracks | {track_count:,} |
| HOTCUE=2 set (Cue 3) | {hc2:,} |
| HOTCUE=3 set (Cue 4) | {hc3:,} |
| autocue_progress done list | {ap_done:,} |

## Run this

You are Claude on the Skippy PC. Place Cue 3 (first vocal/melodic onset)
and Cue 4 (main drop loop) via librosa.

1. `cd D:\\Aaron\\development\\music-collection && git pull`
2. `pip install librosa soundfile numpy numba` (if missing)
3. `python tools\\stage10_autocue.py --all --apply`
4. Commit `corrected_traktor/collection.nml` and `state/autocue_progress.json`
   every 2 hours or on clean exit; push to origin.
5. Report back:
   - Tracks processed this run
   - New HOTCUE=2 / HOTCUE=3 counts (deltas from {hc2} / {hc3})
   - Any errors

Permission: bypass mode. Resumable — safe to interrupt.
""",

    "art": """# PC Dispatch — Album Art Pass

Auto-generated {timestamp}.

## Baseline at dispatch time

| Metric | Count |
|---|---:|
| Tracks with art | {art_found:,} |
| Null entries (couldn't find) | {art_nulls:,} |

## Run this

You are Claude on the Skippy PC. Run the album art pipeline.

1. `cd D:\\Aaron\\development\\music-collection && git pull`
2. `python tools/fetch_album_art.py --report`
3. `python tools/fetch_album_art.py --run --force` (retry nulls with iTunes+MB fallbacks)
4. `python tools/fetch_album_art.py --embed` (write art into audio file tags so Traktor sees it)
5. Commit `state/album_art_index.json`; push to origin. The JPEGs themselves
   sync via Syncthing automatically.
6. Report back: new art-found count, how many files got art embedded.

Permission: bypass mode.
""",

    "status": """# PC Dispatch — Status Check (read-only)

Auto-generated {timestamp}.

## Run this

You are Claude on the Skippy PC. Audit current state and report back.
**No work to perform — strictly read-only.**

1. `cd D:\\Aaron\\development\\music-collection && git pull`
2. Report:
   - `state/lyrics_dedup.json` entries and `theme=instrumental` count
   - `state/autocue_progress.json` "done" list length
   - NML HOTCUE slot distribution (counts for HOTCUE=-1 through HOTCUE=7)
   - Disk free space on the D: drive (`Get-PSDrive D`)
   - Currently-running Python processes (`tasklist /fi "imagename eq python.exe"`)
   - Uptime since any running job started (check `state/stt*progress.json` and
     `state/autocue_progress.json` mtimes)
3. Write findings to `state/pc_status_{short}.json` and commit + push.

Do not modify the NML or any state file other than the status file you create.
""",

    "all": """# PC Dispatch — Full Run (STT + Autocue + Art)

Auto-generated {timestamp}. All three long-running jobs in parallel.

## Baseline at dispatch time

| Metric | Count |
|---|---:|
| NML tracks | {track_count:,} |
| lyrics_dedup entries | {lyrics_total:,} |
| theme=instrumental | {lyrics_instr:,} |
| STT gap (estimate) | ~{lyrics_gap:,} |
| HOTCUE=2 (Cue 3) | {hc2:,} |
| HOTCUE=3 (Cue 4) | {hc3:,} |
| Art found | {art_found:,} |
| Art nulls | {art_nulls:,} |

## Run this

You are Claude on the Skippy PC. Launch all three jobs in parallel —
librosa is CPU-bound, Whisper is GPU-bound, art-fetch is network-bound;
they don't contend.

1. `cd D:\\Aaron\\development\\music-collection && git pull`
2. Start **Job A — autocue**: `python tools\\stage10_autocue.py --all --apply`
   in the background; note PID.
3. Start **Job B — STT**: `python stage9_stt.py --run` in the background;
   note PID.
4. Start **Job C — art**: run in foreground, fast:
   - `python tools/fetch_album_art.py --run --force`
   - then `python tools/fetch_album_art.py --embed`
5. Commit all touched state files + NML every 2 hours or on clean exits;
   push to origin.
6. Report back every 2 hours: PIDs, deltas, any errors.

Permission: bypass mode. All three jobs are resumable.
""",
}


# ── Rendering / file I/O ─────────────────────────────────────────────────────
def render(job: str, baseline: dict, notes: str = "") -> str:
    body = TEMPLATES[job].format(**baseline)
    if notes:
        body += f"\n---\n\n## Operator notes\n\n{notes}\n"
    return body


def dispatch_path(job: str, short: str) -> Path:
    return TOOLS / f"DISPATCH_PC_{job.upper()}_{short}.md"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=check,
    )


# ── Entrypoint ───────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate and push a PC dispatch file from a template.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("job", choices=list(TEMPLATES.keys()),
                    help="Which dispatch to generate")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print to stdout without writing or committing")
    ap.add_argument("--no-push", action="store_true",
                    help="Commit locally but skip the push")
    ap.add_argument("--notes", default="",
                    help="Optional notes appended to the dispatch body")
    args = ap.parse_args()

    baseline = compute_baseline()
    body = render(args.job, baseline, args.notes)

    if args.dry_run:
        print(body)
        return 0

    path = dispatch_path(args.job, baseline["short"])
    path.write_text(body, encoding="utf-8")
    rel = path.relative_to(REPO)
    print(f"[dispatch] wrote {rel}")

    git("add", str(rel))
    msg = f"dispatch: PC — {args.job} ({baseline['timestamp']})"
    r = git("commit", "-m", msg, check=False)
    if r.returncode != 0:
        sys.stderr.write(f"[dispatch] commit failed:\n{r.stderr}\n{r.stdout}")
        return 1
    print(f"[dispatch] committed: {msg}")

    if args.no_push:
        print("[dispatch] --no-push: staying local")
        return 0

    r = git("push", check=False)
    if r.returncode != 0:
        sys.stderr.write(f"[dispatch] push failed:\n{r.stderr}\n{r.stdout}")
        return 1
    print("[dispatch] pushed to origin — PC watcher should pick up within 60s.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
