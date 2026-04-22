# Future Tasks — Music Organize / DJ Block Planner

Logged here when out of scope for the current session. Each entry has enough
context to implement cold.

---

## TASK: Auto Cue Point Setter
**Logged:** Session 5, April 19, 2026
**Status:** Not started

### What to build

A script (or stage) that analyzes audio files with no cue points set in Traktor
and automatically places four cue points:

| Slot | Meaning | Detection method |
|------|---------|-----------------|
| **2** | First beat | Onset detection — first strong transient after any silence/intro |
| **3** | First vocal entry | Voice activity detection (VAD) — first frame where a vocal frequency signature appears |
| **4** | Hardest beat (loop anchor) | Peak RMS/onset energy — strongest transient in the track; snapped to the nearest 4-beat grid boundary. This becomes a 4-grid-space loop point. |
| **8** | 16 grid spaces from end | Calculate track grid length in beats, subtract 16 beats from the last beat, snap to grid. Used for outro/blend timing. |

### NML cue point format

Cue points live in the NML ENTRY as `<CUE_V2>` child elements. Key attributes:

```xml
<CUE_V2 NAME="Cue 2" DISPL_ORDER="0" TYPE="0" START="12345.678"
        LEN="0.000000" REPEATS="-1" HOTCUE="2"/>
```

- `START` = position in **milliseconds** from the start of the file
- `HOTCUE` = the hotcue button number (2, 3, 4, 8 in our case)
- `TYPE="0"` = regular cue; `TYPE="4"` = loop cue
- Cue 4 (loop anchor) should use `TYPE="4"` with `LEN` = length of 4 beats in ms
- `NAME` is free text shown in Traktor

### Algorithm sketch

```
1. Parse NML — find ENTRY nodes with no CUE_V2 children
2. For each such track:
   a. Load audio with librosa (or soundfile for speed)
   b. Detect BPM / beat grid (librosa.beat.beat_track)
   c. Cue 2 — first beat onset: beats[0] converted to ms
   d. Cue 3 — vocal onset: run librosa.effects.split or a simple
              spectral centroid / MFCC classifier to find first
              frame with voice energy above threshold
   e. Cue 4 — hardest beat: beats[argmax(onset_strength[beat_frames])]
              rounded to nearest 4-beat boundary; LEN = 4 * beat_period_ms
   f. Cue 8 — 16 beats from end: beats[-1] - 16 * beat_period_ms
3. Write CUE_V2 elements into NML entries
4. Save NML — validate with xmllint before overwriting
```

### Dependencies

```
pip install librosa soundfile numpy
# Optional for better vocal detection:
pip install pyannote.audio   # full speaker diarization, heavier
# or simpler: librosa spectral_rolloff / zero_crossing_rate heuristic
```

### Scope notes

- **Only process tracks with zero existing cue points** — never overwrite manually placed cues
- Apply to `corrected_traktor/collection.nml` and live `Traktor 4.0.2/collection.nml`
- Add `--dry-run` flag to preview positions without writing
- Add `--limit N` for incremental processing (collection is 13k+ tracks)
- PC-side job candidate — GPU not needed for librosa beat tracking, but it's slow at scale; batch overnight

### Open questions

- Vocal detection accuracy on heavy industrial/EBM tracks — many have no conventional "vocals"
  (synthesizer leads, heavily processed speech). May need genre-conditional logic:
  skip vocal cue for Pure Instrumental genres, use "first melodic element" instead.
- Loop cue (slot 4): Traktor's loop length is quantized to power-of-2 beat counts.
  4 grid spaces = 1 bar at 4/4. Confirm this matches Traktor's `TYPE="4"` LEN expectation.
- Grid alignment: if librosa's beat grid drifts from Traktor's analyzed grid, cues may
  land off-beat. Consider reading Traktor's own TEMPO/GRID data from NML as the
  authoritative beat grid instead of re-detecting.

---

---

## PROJECT: PC as Remote MCP Server
**Logged:** Session 6, April 20, 2026
**Status:** Not started — future project

### Idea

The PC (with its 4070 Ti and always-on availability) becomes a remote MCP
(Model Context Protocol) server accessible from the Mac and the Captain's phone.

**What this enables:**
- Run heavy audio analysis (librosa, Whisper STT) on the PC's GPU/CPU
  remotely from Claude on the Mac — no waiting at the desk
- Issue commands from the phone: "run the overnight autocue pass", "fetch lyrics
  for new tracks added today"
- Claude on any device can dispatch work to the PC via MCP tool calls

**Components to build:**
1. An MCP server running on the PC (Python, `mcp` package from Anthropic)
2. Expose tools: `run_autocue()`, `fetch_lyrics()`, `check_progress()`, `get_setlist()`
3. Tunnel: Tailscale or ngrok so the PC is reachable from outside the LAN
4. Authentication: shared secret or Tailscale ACLs

**Precedent:** The dispatch files (PC_AUTOCUE_TASK.md, DISPATCH_PC_LYRICS.md)
are manual versions of this. The MCP server automates the dispatch entirely.

