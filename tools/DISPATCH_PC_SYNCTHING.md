# PC Dispatch: Set Up Syncthing for Mac↔PC Sync

Syncthing is installed and running on the Mac. This dispatch sets it up on the
PC so the two machines sync automatically over LAN (100+ MB/s) — no internet
required, works even at home with no cloud involved.

---

## Mac device ID (you'll need this)

```
SDI5NCI-FESHIE6-BJEQ6OV-IX5HQXY-LPOSOID-77EV7N4-OXXN24H-HBRNTAS
```

---

## Step 1 — Install Syncthing on Windows

Download the Windows installer from https://syncthing.net/downloads/
or use winget:

```powershell
winget install Syncthing.Syncthing
```

Start it:
```powershell
syncthing.exe
```

It will open a browser tab at http://127.0.0.1:8384 — that's the GUI.

To run as a background service (auto-start on login):
```powershell
# In an admin PowerShell:
syncthing.exe --no-browser install-service
Start-Service syncthing
```

---

## Step 2 — Get the PC's device ID

In the Syncthing GUI: **Actions → Show ID**

Or via CLI:
```powershell
syncthing.exe --device-id
```

**Commit this to git so the Mac agent can complete the pairing:**

```powershell
# Create the pairing file
echo "PC_SYNCTHING_ID=<YOUR-DEVICE-ID-HERE>" > tools/SYNCTHING_PC_ID.txt
git add tools/SYNCTHING_PC_ID.txt
git commit -m "syncthing: add PC device ID for Mac pairing"
git push
```

---

## Step 3 — Add the Mac as a remote device

In the Syncthing GUI:
1. Click **"Add Remote Device"**
2. Device ID: `SDI5NCI-FESHIE6-BJEQ6OV-IX5HQXY-LPOSOID-77EV7N4-OXXN24H-HBRNTAS`
3. Name: `Mac`
4. Click **Save**

---

## Step 4 — Add the shared folders

Add these two folders in the Syncthing GUI (**"Add Folder"**):

### Folder 1: DJ Traktor Library
| Field | Value |
|-------|-------|
| Folder ID | `music-traktor` |
| Folder Label | `DJ Traktor Library` |
| Folder Path | `D:\Aaron\development\music-collection\corrected_traktor` |
| Folder Type | Send & Receive |
| Share with | Mac (device above) |

### Folder 2: DJ State & Cache
| Field | Value |
|-------|-------|
| Folder ID | `music-state` |
| Folder Label | `DJ State & Cache` |
| Folder Path | `D:\Aaron\development\music-collection\state` |
| Folder Type | Send & Receive |
| Share with | Mac (device above) |

**Use the exact Folder IDs** — they must match the Mac's IDs for Syncthing
to recognize them as the same folders.

---

## Step 5 — Signal the Mac

Once Syncthing is running and the PC device ID is committed to git, the Mac
agent will:
1. Pull the PC device ID from git
2. Add the PC as a remote device on the Mac side
3. Share both folders with the PC
4. Confirm the sync link is established

---

## What syncs

| Folder | Direction | Contents |
|--------|-----------|----------|
| `corrected_traktor/` | Both ways | collection.nml, NML backups |
| `state/` | Both ways | autocue_progress.json, lyrics_dedup.json, all caches |

`corrected_music/` (136GB audio) is NOT in Syncthing — it's being uploaded to
Google Drive (`DJ Collection/music/`) for the PC to download once. After that,
neither machine modifies audio files so no sync needed.

---

## Notes

- Syncthing works on LAN without internet — ideal for show prep at home
- First sync of `state/` may take a few minutes (large JSON files)
- The Mac's Syncthing GUI is at http://127.0.0.1:8384
- Both machines must be on to sync — if PC is off, changes queue up on Mac
  and sync when PC wakes up
