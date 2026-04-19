# PC Dispatch: Lyrics Analyzer

Paste this entire file into Claude Code on the PC.

---

You are running on the powerful analysis PC. Fetch lyrics for a 20k-track goth DJ collection, summarize each song with Qwen2.5:15b, and push results back to GitHub.

## Step 1 — Clone or pull the repo

```bash
# First time (Windows — Git Bash or PowerShell):
git clone https://github.com/aaronsrhodes2/music-organizer-manydeduptrak.git "D:/Aaron/development/music-collection"
cd "D:/Aaron/development/music-collection"

# Already cloned:
cd "D:/Aaron/development/music-collection" && git pull
```

## Step 2 — Check Ollama models

```bash
ollama list
```

Use the largest/best available model. Preferred: `qwen2.5:15b`.
If not installed: `ollama pull qwen2.5:15b`

## Step 3 — Check what needs processing

```bash
cd ~/music-organizer
python3 tools/lyrics_analyzer_pc.py --report
```

This shows how many tracks have lyrics cached, how many need summarizing, and how many are already done.

## Step 4 — Run the full pipeline

```bash
python3 tools/lyrics_analyzer_pc.py --model qwen2.5:15b --workers 4 --push
```

**What this does:**
- Reads `state/tracklist.json` — 20,192 unique tracks (artist + title)
- Fetches lyrics from lyrics.ovh for each track (~40% hit rate expected for underground goth)
- Summarizes each song in one sentence using Qwen2.5:15b running locally
- Flags content that conflicts with goth community values (racism, homophobia, etc.)
- Dark themes (death, horror, occultism, BDSM) are NOT flagged — that's just goth
- Saves progress every 25 tracks — safe to interrupt and resume
- `--push` commits `state/lyrics_dedup.json` to GitHub when done

**Tuning workers:**
- `--workers 4` = 4 parallel Ollama calls
- Increase to `--workers 6` or `--workers 8` if GPU can handle it
- Watch GPU VRAM: `nvidia-smi` or `watch -n1 nvidia-smi`

## Step 5 — If interrupted

Just rerun the same command. Already-processed tracks are skipped automatically.

## Step 6 — When done

The Mac will run:
```bash
cd ~/development/music\ organize && git pull
```
The server picks up the new summaries on next reload (or hot-reloads automatically if running).

## Estimated time

- ~8k tracks will get lyrics (40% of 20k)
- At 4 workers + qwen2.5:15b ≈ 3-6 hours total
- At 8 workers it could be 2-3 hours

## Progress output

The script prints live progress every 25-50 tracks:
```
  [250/8000] 12 flagged — 5.2h remaining
```
You'll see it updating in real-time in the terminal.
