#!/usr/bin/env python3
"""
DJ Block Planner — macOS menu bar app.

Sits in the menu bar as a ♪ icon. Click to open/focus the browser window.
Shows the currently playing track as the menu title when a deck is active.
Starts the Flask server automatically if it isn't already running.

Usage:
    python3 menubar_app.py
"""

import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

import rumps

SERVER_URL  = "http://localhost:7334"
SERVER_PORT = 7334
PROJECT_DIR = Path(__file__).parent
SERVER_SCRIPT = PROJECT_DIR / "stage9_dj_suggest.py"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
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
        # Wait for it to come up
        for _ in range(20):
            time.sleep(0.5)
            if server_is_up():
                break


def open_browser():
    subprocess.Popen(
        [CHROME, f"--app={SERVER_URL}", f"--window-size={WINDOW_SIZE}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class DJPlannerApp(rumps.App):
    def __init__(self):
        super().__init__("♪", quit_button=None)
        self.menu = [
            rumps.MenuItem("Open DJ Planner", callback=self.open_planner),
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
            self.title = "♪…"
            start_server()
        open_browser()
        self.title = "♪"

    def restart_server(self, _):
        subprocess.run(["pkill", "-f", "stage9_dj_suggest.py"], capture_output=True)
        time.sleep(1)
        start_server()
        self.title = "♪"

    def _poll_loop(self):
        """Poll /api/deck-status every 3s and update menu title."""
        while True:
            try:
                import json
                with urlopen(f"{SERVER_URL}/api/deck-status", timeout=2) as r:
                    d = json.loads(r.read())
                # Find the playing deck
                if d.get("a") and d.get("playing_a"):
                    t = d["a"]
                    label = f"  A ▶  {t['artist']} — {t['title']}"
                    self.title = "▶ ♪"
                elif d.get("b") and d.get("playing_b"):
                    t = d["b"]
                    label = f"  B ▶  {t['artist']} — {t['title']}"
                    self.title = "▶ ♪"
                elif d.get("a") or d.get("b"):
                    t = d.get("a") or d.get("b")
                    label = f"  {t['artist']} — {t['title']}"
                    self.title = "♪"
                else:
                    label = "  No track loaded"
                    self.title = "♪"
                self._now_playing_item.title = label[:60]
            except Exception:
                self.title = "♪"
                self._now_playing_item.title = "  Server offline"
            time.sleep(3)


if __name__ == "__main__":
    # Ensure server is running before the app loop starts
    if not server_is_up():
        t = threading.Thread(target=start_server, daemon=True)
        t.start()
    DJPlannerApp().run()
