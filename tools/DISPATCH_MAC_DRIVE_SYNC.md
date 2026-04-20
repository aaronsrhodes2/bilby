# Mac Dispatch: Google Drive NML Sync

**Task:** Upload `corrected_traktor/collection.nml` to Google Drive so the PC can
download it, run the Stage 10 audio cue pass (Cues 3 + 4 via librosa), and push
the updated NML back. You then pull it from Drive and apply it to Traktor.

---

## The sync folder

| Field | Value |
|-------|-------|
| **Folder name** | `music-collection-sync` |
| **Drive folder ID** | `12suNo-r5u324rfSOOJJSrX8Cbr32-egB` |
| **View URL** | https://drive.google.com/drive/folders/12suNo-r5u324rfSOOJJSrX8Cbr32-egB |

---

## Step 1 — Upload the corrected NML from Mac

Install the Drive client library if needed:

```bash
pip3 install google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Then run this one-shot upload script:

```python
# upload_nml_to_drive.py  (run once from Mac)
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import json
from pathlib import Path

SCOPES       = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_ID    = "12suNo-r5u324rfSOOJJSrX8Cbr32-egB"
NML_PATH     = Path("corrected_traktor/collection.nml")
TOKEN_FILE   = Path("state/drive_token_mac.json")
CREDS_FILE   = Path("state/drive_credentials.json")

# Auth (same credentials file as drive_intake.py)
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

# Check if file already exists in the sync folder
existing = service.files().list(
    q=f"name='collection.nml' and '{FOLDER_ID}' in parents and trashed=false",
    fields="files(id, name)",
).execute().get("files", [])

media = MediaFileUpload(str(NML_PATH), mimetype="application/xml", resumable=True)

if existing:
    # Update in-place
    file_id = existing[0]["id"]
    service.files().update(fileId=file_id, media_body=media).execute()
    print(f"Updated collection.nml (id={file_id})")
else:
    # Create new
    meta = {"name": "collection.nml", "parents": [FOLDER_ID]}
    f    = service.files().create(body=meta, media_body=media, fields="id").execute()
    print(f"Uploaded collection.nml (id={f['id']})")
```

```bash
python3 upload_nml_to_drive.py
```

---

## Step 2 — PC runs Stage 10 audio pass

The PC will:
1. Download `collection.nml` from the sync folder
2. Run `python tools/stage10_autocue.py --all --apply --audio-root "D:/Aaron/Music/VERAS SONGS"`
3. Re-upload the updated NML back to the same folder

The PC agent handles this automatically once the NML is in Drive.

---

## Step 3 — Pull updated NML back on Mac

After the PC finishes (check git for a commit like "cues: add vocal + drop cues (audio pass)"),
download the updated NML from Drive and apply it:

```bash
# Download updated NML from Drive (same script, reversed)
# Then run the library swap:
cat switch_library.md   # for the Traktor reload procedure
```

---

## Notes

- The `state/drive_credentials.json` OAuth2 file is shared between Mac and PC.
  Copy it from the Google Cloud Console if the Mac doesn't have it yet.
- The token file is machine-specific (`drive_token_mac.json` on Mac vs
  `drive_token.json` on PC) — don't overwrite each other's tokens.
- The sync folder is writable by anyone with the credentials — no sharing
  settings need to change.
