# Mac Dispatch: Upload corrected_traktor + corrected_music to Google Drive

Upload both library folders to the shared **DJ Collection** folder on Drive so
the PC can download them, run the audio cue pass, and push results back.

---

## Drive folder layout

```
DJ Collection/                   (root — ID: 1I0tFUj_7IwjoRb7qwxO7sVlPcmYJAf0D)
  traktor/                       (ID: 1WhSmhI6P_BQlhkea74qZKUv2d6WMwcyI)
    collection.nml               ← upload corrected_traktor/collection.nml here
  music/                         (ID: 1OJlpxL4V9VQcZPr4Jmb5ZQJ8vRXR5-YJ)
    {Artist}/                    ← mirror corrected_music/ tree here
      ...
```

View in Drive: https://drive.google.com/drive/folders/1I0tFUj_7IwjoRb7qwxO7sVlPcmYJAf0D

---

## Step 1 — Install dependencies (once)

```bash
pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

---

## Step 2 — Upload corrected_traktor/collection.nml → traktor/

```python
# upload_traktor_to_drive.py
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from pathlib import Path

SCOPES      = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_ID   = "1WhSmhI6P_BQlhkea74qZKUv2d6WMwcyI"   # DJ Collection/traktor
NML_PATH    = Path("corrected_traktor/collection.nml")
TOKEN_FILE  = Path("state/drive_token_mac.json")
CREDS_FILE  = Path("state/drive_credentials.json")

creds = None
if TOKEN_FILE.exists():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())

service = build("drive", "v3", credentials=creds)

existing = service.files().list(
    q=f"name='collection.nml' and '{FOLDER_ID}' in parents",
    fields="files(id)"
).execute().get("files", [])

media = MediaFileUpload(str(NML_PATH), mimetype="application/xml", resumable=True)

if existing:
    service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    print("Updated collection.nml")
else:
    meta = {"name": "collection.nml", "parents": [FOLDER_ID]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    print(f"Uploaded collection.nml (id={f['id']})")
```

```bash
python3 upload_traktor_to_drive.py
```

---

## Step 3 — Upload corrected_music/ → music/

This mirrors the full corrected_music directory tree into `DJ Collection/music/`.
Run once; subsequent runs skip already-uploaded files (tracked by name + parent).

```python
# upload_music_to_drive.py
import os
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from pathlib import Path

SCOPES      = ["https://www.googleapis.com/auth/drive.file"]
MUSIC_ROOT  = Path("corrected_music")
ROOT_FOLDER = "1OJlpxL4V9VQcZPr4Jmb5ZQJ8vRXR5-YJ"   # DJ Collection/music
TOKEN_FILE  = Path("state/drive_token_mac.json")
CREDS_FILE  = Path("state/drive_credentials.json")

AUDIO_EXTS  = {".mp3", ".flac", ".aiff", ".m4a", ".wav", ".ogg"}

creds = None
if TOKEN_FILE.exists():
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow  = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json())

service = build("drive", "v3", credentials=creds)

def get_or_create_folder(name: str, parent_id: str) -> str:
    res = service.files().list(
        q=f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder'",
        fields="files(id)"
    ).execute().get("files", [])
    if res:
        return res[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    return service.files().create(body=meta, fields="id").execute()["id"]

def file_exists(name: str, parent_id: str) -> bool:
    res = service.files().list(
        q=f"name='{name}' and '{parent_id}' in parents",
        fields="files(id)"
    ).execute().get("files", [])
    return bool(res)

uploaded = skipped = 0
for fpath in sorted(MUSIC_ROOT.rglob("*")):
    if fpath.is_dir() or fpath.suffix.lower() not in AUDIO_EXTS:
        continue
    if fpath.name.startswith("._"):
        continue

    # Ensure parent folder chain exists on Drive
    rel_parts = fpath.relative_to(MUSIC_ROOT).parent.parts
    parent_id = ROOT_FOLDER
    for part in rel_parts:
        parent_id = get_or_create_folder(part, parent_id)

    if file_exists(fpath.name, parent_id):
        skipped += 1
        continue

    mime = "audio/mpeg" if fpath.suffix.lower() == ".mp3" else "audio/x-audio"
    media = MediaFileUpload(str(fpath), mimetype=mime, resumable=True)
    meta  = {"name": fpath.name, "parents": [parent_id]}
    service.files().create(body=meta, media_body=media, fields="id").execute()
    uploaded += 1
    print(f"  ↑ {fpath.relative_to(MUSIC_ROOT)}")

print(f"\nDone. Uploaded: {uploaded}  Skipped (already on Drive): {skipped}")
```

```bash
python3 upload_music_to_drive.py
```

---

## After upload — signal the PC

Commit and push so the PC agent sees the data is ready:

```bash
git add -A && git commit -m "drive: upload corrected NML + music to DJ Collection"
git push
```

The PC will:
1. Download `DJ Collection/traktor/collection.nml`
2. Download `DJ Collection/music/` audio files it needs for cue analysis
3. Run the audio cue pass
4. Push `state/cue_data.json` back to git

---

## Notes

- Token file is machine-specific: `drive_token_mac.json` on Mac, `drive_token.json` on PC
- `state/drive_credentials.json` OAuth2 file is shared — copy from Google Cloud Console if missing
- The old `music-collection-sync` folder (ID: `12suNo-r5u324rfSOOJJSrX8Cbr32-egB`) is obsolete — use `DJ Collection` going forward
