#!/usr/bin/env python3
"""
Maker Shaker — macOS menu bar app for the DJ Block Planner.

Sits in the menu bar with a vinyl record icon. Click to open/focus the
browser window. Shows the currently playing track in the dropdown.
Starts the Flask server automatically if it isn't running.

Usage:
    python3 menubar_app.py
"""

import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

import rumps

APP_NAME    = "Maker Shaker"
SERVER_URL  = "http://localhost:7334"
PROJECT_DIR = Path(__file__).parent
SERVER_SCRIPT = PROJECT_DIR / "stage9_dj_suggest.py"
ICON_PATH   = str(PROJECT_DIR / "misc" / "menubar_icon.png")
WINDOW_SIZE = "1100,900"


def server_is_up() -> bool:
    try:
        with urlopen(f"{SERVER_URL}/api/count", timeout=2):
            return True
    except Exception:
        return False


def start_server():
    if not server_is_up():
        subprocess.Popen(
            ["python3", str(SERVER_SCRIPT)],
            cwd=str(PROJECT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(20):
            time.sleep(0.5)
            if server_is_up():
                break


def focus_or_open_browser():
    """Bring the Maker Shaker Chrome window to front, or open a new one."""
    # Try to focus an existing app-mode window via AppleScript
    script = f'''
    tell application "Google Chrome"
        set found to false
        repeat with w in windows
            repeat with t in tabs of w
                if URL of t contains "localhost:7334" then
                    set index of w to 1
                    activate
                    set found to true
                    exit repeat
                end if
            end repeat
            if found then exit repeat
        end repeat
        if not found then
            open location "{SERVER_URL}"
        end if
    end tell
    tell application "Google Chrome" to activate
    '''
    result = subprocess.run(["osascript", "-e", script],
                            capture_output=True, text=True)
    # If Chrome had no matching window, open app-mode window
    if result.returncode != 0 or "not found" in result.stderr:
        subprocess.Popen(
            ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
             f"--app={SERVER_URL}", f"--window-size={WINDOW_SIZE}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


class MakerShaker(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, icon=ICON_PATH, template=True, quit_button=None)
        self.menu = [
            rumps.MenuItem("Open Maker Shaker", callback=self.open_planner),
            rumps.separator,
            rumps.MenuItem("Now Playing:"),
            rumps.MenuItem("  —"),
            rumps.separator,
            rumps.MenuItem("Restart Server", callback=self.restart_server),
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.menu["Now Playing:"].set_callback(None)
        self.menu["  —"].set_callback(None)
        self._now_playing_item = self.menu["  —"]
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def open_planner(self, _=None):
        if not server_is_up():
            start_server()
        focus_or_open_browser()

    def restart_server(self, _):
        subprocess.run(["pkill", "-f", "stage9_dj_suggest.py"], capture_output=True)
        time.sleep(1)
        start_server()

    def _poll_loop(self):
        while True:
            try:
                with urlopen(f"{SERVER_URL}/api/deck-status", timeout=2) as r:
                    d = json.loads(r.read())
                if d.get("a") and d.get("playing_a"):
                    t = d["a"]
                    self._now_playing_item.title = f"  A ▶  {t['artist']} — {t['title']}"[:60]
                elif d.get("b") and d.get("playing_b"):
                    t = d["b"]
                    self._now_playing_item.title = f"  B ▶  {t['artist']} — {t['title']}"[:60]
                elif d.get("a") or d.get("b"):
                    t = d.get("a") or d.get("b")
                    self._now_playing_item.title = f"  {t['artist']} — {t['title']}"[:60]
                else:
                    self._now_playing_item.title = "  No track loaded"
            except Exception:
                self._now_playing_item.title = "  Server offline"
            time.sleep(3)


if __name__ == "__main__":
    if not server_is_up():
        threading.Thread(target=start_server, daemon=True).start()
    MakerShaker().run()
