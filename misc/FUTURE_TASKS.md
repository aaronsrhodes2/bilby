# Future Tasks — Music Organize / Mac Bilby

Logged here when out of scope for the current session. Each entry has enough
context to implement cold.

---

## TASK: Auto Cue Point Setter
**Logged:** Session 5, April 19, 2026
**Status:** Substantially implemented — compute_cues_pc.py (batch), add_track.py step 12-13 (per-track). Remaining: batch pass for existing library tracks with no cues.

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

---

## PROJECT: Agentic DJ — Bilby Replaces Traktor as Decision-Maker
**Logged:** Session 12, April 24, 2026
**Status:** Future project — vision captured, not started

### Vision

Bilby stops being a library browser and becomes the actual DJ. Traktor keeps running
(it handles audio routing, the S2 Mk3 hardware, and the sound engine) but Bilby drives
it — deciding what plays next, when to transition, how to crossfade. The human DJs by
adjusting parameters and vibes, not by manually cueing tracks.

### Architecture

```
Mac Bilby (brain)
    ↓  OSC messages
Traktor 4 (sound engine + hardware interface)
    ↓  MIDI/HID
Traktor S2 Mk3 (faders, jogs, EQ, headphone cue)
    ↓  audio
PA / headphones
```

Traktor's OSC input is the control surface. Bilby sends commands; Traktor executes them.
The S2 Mk3 knobs/faders remain usable for human override at any time.

### Traktor OSC Commands (core set)

Traktor accepts OSC on configurable port (default: 3001). Key addresses:

| OSC Address | Args | Effect |
|---|---|---|
| `/deck/a/load_track` | `(string) filepath` | Load track into deck A |
| `/deck/b/load_track` | `(string) filepath` | Load track into deck B |
| `/deck/a/play` | `(int) 1\|0` | Play / pause deck A |
| `/deck/b/play` | `(int) 1\|0` | Play / pause deck B |
| `/deck/a/tempo` | `(float) bpm` | Set deck A tempo |
| `/deck/b/tempo` | `(float) bpm` | Set deck B tempo |
| `/deck/a/tempo_sync` | `(int) 1\|0` | Enable/disable sync on A |
| `/deck/a/cue_point` | `(int) n` | Jump to hotcue n |
| `/crossfader` | `(float) -1.0–1.0` | Move crossfader (-1=A, 0=centre, 1=B) |
| `/deck/a/volume` | `(float) 0.0–1.0` | Deck A channel fader |
| `/deck/b/volume` | `(float) 0.0–1.0` | Deck B channel fader |
| `/deck/a/elapsed_time` | — | Read-only: current playback position (ms) — already used in karaoke |

Python binding: `python-osc` (`pip install python-osc`).

```python
from pythonosc import udp_client
osc = udp_client.SimpleUDPClient("127.0.0.1", 3001)

osc.send_message("/deck/b/load_track", "/Users/aaron/corrected_music/Artist/Album/track.mp3")
osc.send_message("/deck/b/play", 1)
osc.send_message("/crossfader", 0.0)  # centre
```

### Agentic Decision Loop

Bilby runs an autonomous loop that fires ~every N seconds (configurable):

```
1. Read current state
   - Deck A: elapsed_time, total_time, bpm, track_id
   - Deck B: loaded?, playing?, bpm, track_id
   - Crossfader position
   - Current energy level (derived from BPM + genre + hour of set)

2. Decide: is it time to mix?
   - Heuristic: elapsed >= (total - mix_window), where mix_window = 32–64 bars
   - or: manual trigger from Captain ("go to next track")

3. Select next track
   - Query Bilby's own NML index
   - Filter by: compatible key (Camelot wheel ±1), BPM within ±6%, energy trajectory
   - Rank by: Claude API call — summarise current mood/energy, pick from candidates
   - Optional: Captain can veto or nudge ("something darker")

4. Execute transition
   a. Load chosen track onto the idle deck
   b. Set deck tempo to match current BPM (or drift into the new BPM gradually)
   c. Wait for mix-in cue point (hotcue 2 = "First Beat" in our scheme)
   d. Begin crossfade over N bars (configurable: 4 / 8 / 16 bars)
   e. After fade complete: set old deck volume to 0, stop it, mark it idle

5. Update state, log transition to setlist
```

### NML data used by the decision loop

All already stored in the NML per-ENTRY:

| Field | Source | Used for |
|---|---|---|
| `INFO BPM` | Traktor / autocue | Tempo matching |
| `INFO KEY` | Traktor key detect | Camelot wheel filtering |
| `INFO COMMENT2` | Claude Haiku summary | Mood/energy embedding |
| `INFO KEY_LYRICS` | Lyrics pipeline | Sentiment / lyric theme |
| `INFO GENRE` | MusicBrainz | Genre continuity |
| `CUE_V2 HOTCUE=2` | Autocue vocal-in | Mix-in point |
| `CUE_V2 HOTCUE=3 TYPE=4` | Autocue drop | Energy spike moment |

### Phased Rollout

**Phase 1 — Bilby-Assisted** *(safe, recommended first)*
- Bilby picks the next track and loads it onto the idle deck
- Human still decides when to crossfade and does it manually on the S2 Mk3
- Bilby shows its reasoning in the UI: "Loaded: X — matching key 8A, +2 BPM, same energy"

**Phase 2 — Bilby-Supervised**
- Bilby executes the full crossfade automatically
- Human can abort at any time by touching the crossfader (S2 Mk3 hardware takes priority
  if Traktor is configured with "Fader = Hardware Override")
- Captain reviews transitions after the fact; can veto and force a different track

**Phase 3 — Fully Autonomous**
- Bilby runs the entire set
- Captain sets vibe parameters ("keep it dark industrial, BPM 140–160")
- Human intervention optional — party goes on without DJ presence

### S2 Mk3 Hardware Integration

The S2 Mk3 connects over USB. Traktor sees it as a native controller — all knobs/faders
update Traktor's internal state in real time. OSC commands and hardware control are
non-conflicting: hardware moves are read back as OSC state updates.

Key hardware/OSC interplay:
- **Crossfader**: OSC sets software position. Physical fader overrides if touched.
- **Jog wheels**: For manual nudging tempo during transition — human can "assist"
- **Channel EQs**: Bilby controls via OSC; human can grab knobs at any time
- **Headphone cue**: Bilby loads track into headphone cue (B) before committing to speakers

### Technical Dependencies

```bash
pip install python-osc      # OSC client/server
pip install anthropic        # already installed, for transition decisions
# Optional:
pip install essentia         # higher-quality key/BPM than librosa
```

Traktor setup required:
- Preferences → MIDI Clock + Sync → Enable OSC: port 3001, IP 127.0.0.1
- Map `/deck/a/elapsed_time` and `/deck/b/elapsed_time` to OSC output
  (already done for karaoke feature — same mappings used here)

### Open Questions

- **OSC input vs output in Traktor**: Traktor Pro sends OSC output (elapsed time etc.)
  and accepts OSC input for control. Verify the input address schema matches the
  above table — different Traktor versions use slightly different paths.
  Traktor 4.x may use `/channel/a/` instead of `/deck/a/`.
- **BPM matching accuracy**: Librosa BPM detection has ±2% error. Traktor's own
  BPM analysis is more reliable — read from NML `TEMPO BPM=` attribute for mixing.
- **Key compatibility**: "Camelot wheel" rules are a heuristic, not a law. Some of
  our genres (industrial, EBM, harsh noise) ignore tonality entirely. May need a
  genre-conditional key filter: skip key matching for tracks tagged Pure Instrumental
  or Noise.
- **Claude API for track selection**: Real-time LLM calls per transition (~15s window)
  are feasible but add latency. Consider: pre-embed all COMMENT2 summaries as vectors,
  use cosine similarity for fast candidate shortlist, then one LLM call to rank top 5.
- **Failsafe**: If Bilby crashes mid-set, Traktor keeps playing whatever's loaded.
  Build a watchdog: if no OSC command sent for >5 min, Bilby auto-loads a "safe"
  track from a curated emergency playlist.

---

