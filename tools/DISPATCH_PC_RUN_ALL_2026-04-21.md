# PC Dispatch — Full Run: Autocue (Slots 3+4), STT Lyrics, Instrumental Tags

**Paste the entire "Run this" block at the bottom into Claude on the PC.**
Claude will audit current progress against the baseline below, resume any
partial job, then launch the remaining work in order. All three scripts are
resumable — safe to stop and restart at any point.

---

## Baseline (Mac snapshot, 2026-04-21)

| Metric | Count | Source |
|---|---:|---|
| Tracks in NML | 21,445 | `corrected_traktor/collection.nml` |
| HOTCUE=0 set (Cue 1) | 21,397 | NML `<CUE_V2>` |
| HOTCUE=1 set (Cue 2) | 21,365 | NML |
| HOTCUE=2 set (Cue 3) | **151** ← small, needs job #1 | NML |
| HOTCUE=3 set (Cue 4) | **56** ← small, needs job #1 | NML |
| HOTCUE=7 set (Cue 8) | 21,369 | NML |
| `lyrics_dedup.json` entries | **15,789** ← needs job #2 | `state/lyrics_dedup.json` |
| `theme=instrumental` tagged | **0** ← fills from job #2 | same |
| `autocue_progress.json` done | 23,150 | `state/autocue_progress.json` (slots 1/2/8 only) |

**Expected deltas after all three jobs complete:**
- HOTCUE=2 and HOTCUE=3 reach ≈ 21,000 each (Cue 3, Cue 4 placed by librosa)
- `lyrics_dedup.json` grows to ~20,000+ entries (STT fills the gap for tracks with local audio)
- `theme=instrumental` count lights up for every track Whisper hears no speech on

---

## Job 1 — Autocue Slots 3 & 4 (librosa audio pass)

**Script:** `tools/stage10_autocue.py` (already in repo)
**Purpose:** Places Cue 3 (first vocal / melodic onset) and Cue 4 (main drop loop) on every track that currently has neither. Uses librosa for onset detection and RMS-peak analysis.
**Expected runtime:** ~6–10 hours for the full collection on CPU; librosa is not GPU-accelerated.
**Resumable:** Writes `state/autocue_progress.json` between tracks; re-running picks up exactly where it left off.

```powershell
cd D:\Aaron\development\music-collection
git pull
pip install librosa soundfile numpy numba
python tools\stage10_autocue.py --all --apply
```

Remove `--limit` so it runs the whole collection. Overnight run recommended.

When done:
```powershell
git add corrected_traktor/collection.nml state/autocue_progress.json
git commit -m "pc: autocue slots 3 & 4 — librosa pass complete"
git push
```

---

## Job 2 — STT Lyrics + Instrumental Tags (CUDA Whisper)

**Script:** `stage9_stt.py` (PC version, faster-whisper CUDA)
**Purpose:**
- Transcribes the ~4,400 tracks that have no lyrics entry
- For each, calls Claude Haiku to produce the single-sentence `summary`, `theme`, and `flags`
- Whisper "no speech detected" → `{summary: "Instrumental — no vocals detected.", theme: "instrumental", flags: []}` → the Mac's `is_instrumental()` predicate picks it up automatically (✔ instrumental tagging is a byproduct of this job, not a separate pass)

**Expected runtime:** ~12–18 hours on a 4070 Ti (CUDA faster-whisper at ~8× real-time).
**Resumable:** Writes `state/stt_progress.json` (or equivalent) between tracks.
**Env:** Anthropic API key in `anthropic_creds.txt` (already in `.env`).

```powershell
cd D:\Aaron\development\music-collection
git pull
python stage9_stt.py --report          # audit gap
python stage9_stt.py --run             # full resumable pass
```

When done:
```powershell
git add state/lyrics_dedup.json state/lyrics_raw.json state/stt*progress.json
git commit -m "pc: stt lyrics pass complete — N tracks summarised, M instrumentals tagged"
git push
```

---

## Job 3 — Instrumental Tags (automatic side-effect)

No separate work. Job 2 writes `theme=instrumental` for every silent track; the Mac's DJ Block Planner already reads that field via `is_instrumental()` and renders the `♬ INSTR` badge + violet left-border. Refreshing the browser after the lyrics commit is all that's needed.

---

## Order & Parallelism

Both long jobs can run **in parallel** if the machine has the thermal headroom. librosa is CPU-bound; faster-whisper is GPU-bound; they don't compete. If in doubt, run them sequentially: Job 1 first (less runtime), then Job 2.

---

## Run this — paste into Claude on the PC

> You are Claude running on the Skippy PC (Windows, 4070 Ti, Ollama + faster-whisper installed). The Mac has dispatched three jobs. Audit progress, resume where possible, and launch the remaining work.
>
> 1. `cd D:\Aaron\development\music-collection && git pull`
> 2. Report the current state of:
>     - `corrected_traktor/collection.nml` HOTCUE slot distribution (count tracks with HOTCUE=2 and HOTCUE=3 set — these are the targets of Job 1)
>     - `state/lyrics_dedup.json` entry count and `theme=instrumental` count
>     - `state/autocue_progress.json` "done" list length
>     - Presence and size of `state/stt_progress.json` (or whatever `stage9_stt.py` writes for progress)
> 3. Compare against the baseline table in `tools/DISPATCH_PC_RUN_ALL_2026-04-21.md` and tell the Captain which jobs have already started and how far they've gotten.
> 4. Kick off Job 1 (`python tools\stage10_autocue.py --all --apply`) in the background and note its PID.
> 5. Kick off Job 2 (`python stage9_stt.py --run`) in a second background process and note its PID.
> 6. Report both PIDs, the log file paths, and an ETA for each.
> 7. Do not wait for either to complete. Commit the NML + state files every 2 hours or whenever a job exits cleanly; push to origin.
> 8. If either job errors, capture the last 50 lines of stderr and report back.
>
> Permission: bypass mode — proceed without prompting for each command. If you hit a genuinely unsafe operation (overwriting collection.nml without backup, deleting files, etc.), stop and ask.
