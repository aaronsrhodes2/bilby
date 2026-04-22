# Mac Dispatch: Share corrected_music via Syncthing — URGENT

The PC is ready and waiting. Add corrected_music to Syncthing NOW so the full
23k audio cue pass can start.

PC device ID: `NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL`

---

## One command — run this now

Get your Syncthing API key from http://127.0.0.1:8384 → Actions → Settings,
then run:

```python
# share_music_syncthing.py
import json, urllib.request

API_KEY = "<YOUR-MAC-API-KEY>"
BASE    = "http://127.0.0.1:8384"
PC_ID   = "NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL"

import subprocess, re
result = subprocess.run(
    ["grep", "-oP", "(?<=<apikey>)[^<]+",
     "/Users/aaronrhodes/Library/Application Support/Syncthing/config.xml"],
    capture_output=True, text=True
)
if result.stdout.strip():
    API_KEY = result.stdout.strip()
    print(f"API key: {API_KEY[:8]}...")

folder = {
    "id": "music-corrected",
    "label": "DJ Corrected Music",
    "path": "/Users/aaronrhodes/development/music organize/corrected_music",
    "type": "sendreceive",
    "devices": [{"deviceID": PC_ID, "introducedBy": "", "encryptionPassword": ""}],
    "rescanIntervalS": 3600,
    "fsWatcherEnabled": True,
    "ignorePerms": False,
    "autoNormalize": True,
}

body = json.dumps(folder).encode()
req  = urllib.request.Request(
    f"{BASE}/rest/config/folders/music-corrected",
    data=body, method="PUT",
    headers={"X-API-Key": API_KEY, "Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    print(f"HTTP {r.status} — folder shared with PC")
    print("Syncthing will now push corrected_music to the PC.")
    print("~136 GB at LAN speeds = 20-40 min.")
```

```bash
python3 share_music_syncthing.py
```

---

## What the PC expects

| Field | Value |
|-------|-------|
| Folder ID | `music-corrected` — must match exactly |
| PC local path | `D:\Aaron\Music\corrected_music` |
| Direction | Send & Receive |

---

## After sharing

The PC will automatically begin the full 23k cue point reset once files arrive.
No further action needed — it will detect the sync and restart the run.

Do NOT stop Syncthing or close the lid until the transfer completes.
