#!/usr/bin/env python3
"""
pc_dispatch_watcher.py — autonomous dispatch runner for the Skippy PC.

Pattern:
  Mac pushes a `tools/DISPATCH_PC_*.md` file to git main.
  This watcher (running as a Windows Scheduled Task on the PC) polls the
  remote every 60s, pulls new commits, detects unprocessed dispatches,
  extracts their "## Run this" prompt block, and invokes the Claude CLI
  in bypass-permissions mode with the repo as cwd.

Each dispatch is processed exactly once — state lives in
state/pc_dispatch_log.json. Delete an entry from the log to force reprocess.

Install on the PC (once):
    python tools\\pc_dispatch_watcher.py --install
Uninstall:
    python tools\\pc_dispatch_watcher.py --uninstall
Run in the foreground (for testing):
    python tools\\pc_dispatch_watcher.py --run

Kill switch: push a file called tools/STOP_WATCHER (empty) to git main.
The watcher sees it, exits cleanly, and the scheduled task will retry on
next login. Remove the file from git to re-enable.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
REPO            = Path(__file__).resolve().parent.parent
TOOLS           = REPO / "tools"
STATE           = REPO / "state"
LOG_PATH        = STATE / "pc_dispatch_log.json"
STOP_PATH       = TOOLS / "STOP_WATCHER"
POLL_INTERVAL   = 60         # seconds between git fetches
DISPATCH_RE     = "DISPATCH_PC_*.md"
RUN_MARKER      = "## Run this"
MAX_RUN_SECS    = 30 * 3600  # 30-hour hard cap per dispatch
EXPECTED_REMOTE = "aaronsrhodes2/music-organizer-manydeduptrak"

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [watcher] {msg}", flush=True)

def load_state() -> dict:
    if LOG_PATH.exists():
        try:
            return json.loads(LOG_PATH.read_text())
        except Exception:
            pass
    return {"processed": [], "last_run": None, "started": time.time()}

def save_state(s: dict) -> None:
    STATE.mkdir(exist_ok=True)
    LOG_PATH.write_text(json.dumps(s, indent=2))

def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True, text=True, check=False,
    )

def verify_remote() -> bool:
    r = git("remote", "get-url", "origin")
    if r.returncode != 0:
        log(f"could not read git remote: {r.stderr.strip()}")
        return False
    if EXPECTED_REMOTE not in r.stdout:
        log(f"remote url does not match expected ({EXPECTED_REMOTE}): {r.stdout.strip()}")
        return False
    return True

def pull() -> bool:
    f = git("fetch", "origin", "main")
    if f.returncode != 0:
        log(f"fetch failed: {f.stderr.strip()}")
        return False
    p = git("pull", "--ff-only")
    if p.returncode != 0:
        log(f"pull --ff-only failed (local diverged): {p.stderr.strip()}")
        return False
    return True

def find_unprocessed(state: dict) -> list[Path]:
    seen = set(state.get("processed", []))
    return sorted(
        p for p in TOOLS.glob(DISPATCH_RE)
        if p.name not in seen
    )

def extract_prompt(dispatch: Path) -> str | None:
    """Grab everything from '## Run this' to EOF as the prompt."""
    text = dispatch.read_text(encoding="utf-8", errors="ignore")
    idx = text.find(RUN_MARKER)
    if idx < 0:
        return None
    # Strip the leading header line
    body = text[idx:].split("\n", 1)[1] if "\n" in text[idx:] else text[idx:]
    return body.strip()

def run_dispatch(dispatch: Path, prompt: str) -> int:
    """Invoke Claude CLI with the prompt. Returns exit code."""
    log(f"invoking claude for {dispatch.name}")
    try:
        r = subprocess.run(
            ["claude", "--print", "--dangerously-skip-permissions", prompt],
            cwd=str(REPO),
            capture_output=True, text=True,
            timeout=MAX_RUN_SECS,
        )
        if r.stdout:
            log(f"claude stdout (last 500 chars):\n{r.stdout[-500:]}")
        if r.stderr:
            log(f"claude stderr (last 500 chars):\n{r.stderr[-500:]}")
        return r.returncode
    except FileNotFoundError:
        log("claude CLI not found on PATH. Install Claude Code CLI and re-run.")
        return 127
    except subprocess.TimeoutExpired:
        log(f"dispatch {dispatch.name} exceeded {MAX_RUN_SECS}s — killed")
        return 124

# ── Main loop ─────────────────────────────────────────────────────────────────

def watch_forever() -> None:
    log(f"started. repo={REPO} poll={POLL_INTERVAL}s")
    if not verify_remote():
        log("aborting — remote verification failed")
        return
    state = load_state()
    while True:
        try:
            if STOP_PATH.exists():
                log(f"kill switch detected ({STOP_PATH.name}) — exiting")
                return
            if not pull():
                time.sleep(POLL_INTERVAL)
                continue
            for dispatch in find_unprocessed(state):
                prompt = extract_prompt(dispatch)
                if not prompt:
                    log(f"{dispatch.name}: no '{RUN_MARKER}' block — marking processed and skipping")
                    state.setdefault("processed", []).append(dispatch.name)
                    save_state(state)
                    continue
                t0 = time.time()
                rc = run_dispatch(dispatch, prompt)
                elapsed = time.time() - t0
                state.setdefault("processed", []).append(dispatch.name)
                state["last_run"] = {
                    "file": dispatch.name, "rc": rc,
                    "started": t0, "elapsed_sec": round(elapsed, 1),
                }
                save_state(state)
                log(f"{dispatch.name} finished rc={rc} in {elapsed:.0f}s")
        except Exception as e:
            log(f"loop error: {e!r}")
        time.sleep(POLL_INTERVAL)

# ── Windows scheduled-task installer ──────────────────────────────────────────

TASK_NAME = "MusicOrganizer-DispatchWatcher"

def install_task() -> None:
    if sys.platform != "win32":
        log("--install only supported on Windows. On Linux/Mac, run --run under a supervisor (systemd, launchd, pm2).")
        return
    script = str(Path(__file__).resolve())
    python = sys.executable
    cmd = [
        "schtasks", "/create", "/tn", TASK_NAME,
        "/tr", f'"{python}" "{script}" --run',
        "/sc", "onlogon",
        "/rl", "highest",
        "/f",
    ]
    log(f"installing scheduled task: {TASK_NAME}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        log(f"installed. Task will auto-start next login. To start now:")
        log(f"  schtasks /run /tn {TASK_NAME}")
    else:
        log(f"install failed: {r.stderr or r.stdout}")

def uninstall_task() -> None:
    if sys.platform != "win32":
        return
    r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True, text=True)
    log(r.stdout or r.stderr)

# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run",       action="store_true", help="run the watch loop in the foreground")
    ap.add_argument("--install",   action="store_true", help="register as Windows Scheduled Task (onlogon)")
    ap.add_argument("--uninstall", action="store_true", help="remove the scheduled task")
    ap.add_argument("--status",    action="store_true", help="print the dispatch log and exit")
    args = ap.parse_args()
    if args.install:       install_task()
    elif args.uninstall:   uninstall_task()
    elif args.status:      print(json.dumps(load_state(), indent=2))
    elif args.run:         watch_forever()
    else:                  ap.print_help()
