# PC Dispatch — Autocue Slots 3 & 4 (Full Collection, Hardened Run)

**Dispatched:** 2026-04-22  
**Due:** 2026-04-27 (Sunday)  
**Picked up automatically by the dispatch watcher.**

---

## What changed since last autocue dispatch

The Mac updated `tools/stage10_autocue.py` with three critical fixes:

1. **Bug fix** — previous version computed Cues 3 and 4 but silently discarded
   them before writing to the NML. They are now written correctly.
2. **Stall recovery** — each track runs in a persistent subprocess. If librosa
   hangs past `--timeout` seconds (default 120), the worker is killed and
   restarted, the track is logged as `stalled`, and the run continues.
3. **Keep-awake + low priority** — machine will not sleep mid-run; process
   yields to everything else so you can keep working.

---

## Baseline (Mac snapshot, 2026-04-22)

| Metric | Count |
|---|---:|
| Tracks in NML (`corrected_traktor/collection.nml`) | 23,080 |
| Cue 3 (HOTCUE=2) set | 0 |
| Cue 4 (HOTCUE=3) set | 0 |
| `state/autocue_progress.json` exists | No — fresh start |

---

## Job — Autocue Slots 3 & 4 (full collection)

```powershell
cd D:\Aaron\development\music-collection
git pull
pip install librosa soundfile numpy numba
python tools\stage10_autocue.py --all --apply
```

- No `--limit` — runs the full collection, resumable at any point.
- Progress saved every 100 tracks to `state/autocue_progress.json`.
- Stalled tracks (librosa timeout) logged to `stalled` list; retry later with `--retry-stalled`.
- ETA: ~6–19 hours depending on CPU speed.

When done:
```powershell
git add corrected_traktor/collection.nml state/autocue_progress.json
git commit -m "pc: autocue slots 3+4 — vocal + drop cues, full collection"
git push
```

---

## Run this — extracted by dispatch watcher

> You are Claude running on the Skippy PC (Windows). The Mac has dispatched an
> updated autocue job. Audit current state, then launch it.
>
> 1. `cd D:\Aaron\development\music-collection && git pull`
> 2. Confirm `tools/stage10_autocue.py` has a `TimeoutWorker` class (grep for it).
>    If not, the pull didn't land — stop and report.
> 3. Run the report to confirm baseline:
>    `python tools\stage10_autocue.py --report --nml corrected_traktor\collection.nml`
> 4. Install deps if needed: `pip install librosa soundfile numpy numba`
> 5. Launch the full audio pass in the background:
>    `python tools\stage10_autocue.py --all --apply --nml corrected_traktor\collection.nml`
>    Note the PID. Do NOT use --limit — run the full collection.
> 6. Report the PID and estimated completion time based on the track count.
> 7. When the job completes (or every ~2 hours while running), commit and push:
>    `git add corrected_traktor/collection.nml state/autocue_progress.json`
>    `git commit -m "pc: autocue slots 3+4 — N tracks done"`
>    `git push`
> 8. If any stalled tracks are logged at the end, report the count.
>    They can be retried with `--retry-stalled` in a follow-up run.
>
> Permission: bypass mode — proceed without prompting. If you hit a genuinely
> unsafe operation (overwriting NML without backup, deleting files), stop and ask.
