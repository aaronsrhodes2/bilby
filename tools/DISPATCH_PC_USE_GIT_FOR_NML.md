# PC Dispatch: Switch Traktor NML from Syncthing → Git

The Mac has added `corrected_traktor/` to the git repo. All NML files
(collection.nml + all setlist playlists) are now version-controlled.
Stop relying on Syncthing for the traktor folder — use git pull/push instead.

---

## Step 1 — Pull the NML now

```powershell
cd D:\Aaron\development\music-collection
git pull
```

You will see `corrected_traktor/collection.nml` and all the setlist NMLs
appear (or update). This is the fully cleaned library:
- 21,437 entries (was 23,780)
- 91 ghost entries removed (missing audio files)
- 65 exact-match duplicates removed
- 2,187 artist+title duplicates removed (_2/_3 rename-collision copies)

---

## Step 2 — Disable the music-traktor Syncthing folder

The `music-traktor` Syncthing folder is no longer needed. Pause or remove it
to avoid it overwriting git-managed NML files with stale versions.

In the Syncthing GUI (http://127.0.0.1:8384):
1. Find **DJ Traktor Library** (`music-traktor`)
2. Click it → **Edit** → check **Pause**
3. Save

Or via REST API:
```powershell
$API_KEY = "<your-pc-syncthing-api-key>"
$cfg = Invoke-RestMethod -Uri "http://127.0.0.1:8384/rest/config/folders/music-traktor" `
    -Headers @{"X-API-Key"=$API_KEY}
$cfg.paused = $true
$body = $cfg | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri "http://127.0.0.1:8384/rest/config/folders/music-traktor" `
    -Method PUT -Headers @{"X-API-Key"=$API_KEY} `
    -Body $body -ContentType "application/json"
```

---

## Step 3 — NML workflow going forward

| Action | Command |
|--------|---------|
| Get latest NML from Mac | `git pull` |
| Push NML changes to Mac | `git add corrected_traktor/collection.nml && git commit -m "..." && git push` |

**Syncthing is still active for:**
- `music-corrected` — 136 GB audio (no change, keep running)
- `music-state` — state/ caches (no change, keep running)

Only `music-traktor` is switching to git.

---

## Note on the cue pass

When the audio cue pass writes results back to `collection.nml`, commit
and push so the Mac picks them up via `git pull`. The Mac agent will handle
the merge if both sides have touched the file — just push when the pass is done.
