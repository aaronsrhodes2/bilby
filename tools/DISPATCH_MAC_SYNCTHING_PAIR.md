# Mac Dispatch: Complete Syncthing Pairing with PC

The PC has Syncthing installed and configured. You need to complete the pairing
on the Mac side so the two machines can sync automatically over LAN.

---

## PC device ID

```
NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL
```

(Also in `tools/SYNCTHING_PC_ID.txt` if you want to read it from git.)

---

## Step 1 — Add the PC as a remote device

Open the Syncthing GUI: http://127.0.0.1:8384

1. Click **"Add Remote Device"**
2. Device ID: `NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL`
3. Name: `PC`
4. Click **Save**

Or via the Syncthing CLI / REST API:

```bash
curl -X PUT http://127.0.0.1:8384/rest/config/devices/NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL \
  -H "X-API-Key: $(cat ~/.config/syncthing/config.xml | grep -oP '(?<=<apikey>)[^<]+')" \
  -H "Content-Type: application/json" \
  -d '{
    "deviceID": "NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL",
    "name": "PC",
    "addresses": ["dynamic"],
    "compression": "metadata",
    "autoAcceptFolders": false,
    "paused": false
  }'
```

---

## Step 2 — Share the two folders with the PC

The PC is already configured with these folder IDs. Use the **exact same IDs**
so Syncthing recognises them as the same folders.

### Folder 1: DJ Traktor Library
| Field | Value |
|-------|-------|
| Folder ID | `music-traktor` |
| Folder Label | `DJ Traktor Library` |
| Local path | `corrected_traktor/` (your existing folder) |
| Share with | PC (device above) |

### Folder 2: DJ State & Cache
| Field | Value |
|-------|-------|
| Folder ID | `music-state` |
| Folder Label | `DJ State & Cache` |
| Local path | `state/` (your existing folder) |
| Share with | PC (device above) |

Via REST API (get your API key from the Syncthing GUI → Actions → Settings):

```bash
API_KEY="<your-mac-api-key>"
PC_ID="NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL"
REPO="$(pwd)"  # run from music-collection root

python3 - <<PYEOF
import json, urllib.request

API_KEY = "$API_KEY"
BASE    = "http://127.0.0.1:8384"
PC_ID   = "$PC_ID"
REPO    = "$REPO"

folders = [
    {
        "id": "music-traktor",
        "label": "DJ Traktor Library",
        "path": f"{REPO}/corrected_traktor",
        "type": "sendreceive",
        "devices": [{"deviceID": PC_ID, "introducedBy": "", "encryptionPassword": ""}],
        "rescanIntervalS": 3600,
        "fsWatcherEnabled": True,
        "ignorePerms": False,
        "autoNormalize": True,
    },
    {
        "id": "music-state",
        "label": "DJ State & Cache",
        "path": f"{REPO}/state",
        "type": "sendreceive",
        "devices": [{"deviceID": PC_ID, "introducedBy": "", "encryptionPassword": ""}],
        "rescanIntervalS": 3600,
        "fsWatcherEnabled": True,
        "ignorePerms": False,
        "autoNormalize": True,
    },
]

for folder in folders:
    body = json.dumps(folder).encode()
    url  = f"{BASE}/rest/config/folders/{folder['id']}"
    req  = urllib.request.Request(url, data=body, method="PUT",
                                  headers={"X-API-Key": API_KEY,
                                           "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        print(f"  {folder['id']}: HTTP {r.status}")
PYEOF
```

---

## Step 3 — Confirm the PC is connected

```bash
API_KEY="<your-mac-api-key>"
PC_ID="NGHPMDN-6SL6SIT-544ZP3C-R2SS3K6-6U4BNIO-WMCPFIS-7T2LKCZ-HF3ENQL"

python3 - <<PYEOF
import json, urllib.request
API_KEY = "$API_KEY"
PC_ID   = "$PC_ID"

req = urllib.request.Request(
    f"http://127.0.0.1:8384/rest/system/connections",
    headers={"X-API-Key": API_KEY})
with urllib.request.urlopen(req) as r:
    conns = json.load(r)

pc = conns.get("connections", {}).get(PC_ID, {})
print("PC connected:", pc.get("connected", False))
print("Address:     ", pc.get("address", "not connected"))
PYEOF
```

Once connected, Syncthing syncs over LAN automatically — no internet needed.
The PC will receive `corrected_traktor/collection.nml` and `state/` within minutes.

---

## What syncs

| Folder ID | Mac path | PC path |
|-----------|----------|---------|
| `music-traktor` | `corrected_traktor/` | `D:\Aaron\development\music-collection\corrected_traktor\` |
| `music-state` | `state/` | `D:\Aaron\development\music-collection\state\` |

Note: `corrected_music/` (audio) is handled separately via Google Drive upload —
see `tools/DISPATCH_MAC_DRIVE_SYNC.md`.
