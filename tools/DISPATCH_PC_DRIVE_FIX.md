# PC Dispatch: Fix Stuck Google Drive Upload Queue

## Problem
Google Drive for Desktop has a stuck upload queue (files queued but source
deleted — same issue Mac had for 462 days). This causes the Drive mount to
time out on all operations.

## Fix (takes ~2 minutes)

### Step 1 — Find your DriveFS account folder

Open PowerShell and run:
```powershell
ls "$env:LOCALAPPDATA\Google\DriveFS\"
```
You'll see a folder named with a long numeric ID (e.g. `105768719667086319459`).
That's your account ID. Use it in place of `ACCOUNT_ID` below.

### Step 2 — Kill Drive, nuke cache, restart

```powershell
# Kill Drive for Desktop
Stop-Process -Name "GoogleDriveFS" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

# Full nuke of account cache — Drive rebuilds from Google's servers
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Google\DriveFS\ACCOUNT_ID"

# Restart Drive for Desktop
Start-Process "$env:LOCALAPPDATA\Google\Drive\googledrivesync.exe" -ErrorAction SilentlyContinue
# If that path doesn't work, try:
Start-Process "C:\Program Files\Google\Drive File Stream\launch.bat" -ErrorAction SilentlyContinue
# Or just open Google Drive from the Start menu
```

### Step 3 — Wait for mount to come back (~2 min)

The Drive should mount at `G:\My Drive\` (or wherever it was). Once accessible:
- The stuck upload error will be gone
- The mount will respond normally

## After Drive is working

Once the Drive mount is up, you can sync the music collection from Drive.
The Mac will upload `corrected_traktor/collection.nml` and `corrected_music/`
to the `music-collection-sync` folder on Drive (folder ID: `12suNo-r5u324rfSOOJJSrX8Cbr32-egB`).

Watch for a git commit like:
  `drive: upload corrected NML + music to music-collection-sync`

Then download from your Drive mount:
```powershell
# Copy NML from Drive to local working copy
Copy-Item "G:\My Drive\music-collection-sync\collection.nml" `
          "D:\Aaron\development\music-collection\corrected_traktor\collection.nml"
```

Then run the Stage 10 audio cue pass as per PC_AUTOCUE_TASK.md.
