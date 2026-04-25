# Mac Bilby — Karaoke Display

## What It Does

While a track is playing, Mac Bilby shows a rolling 7-line lyric display in the
anchor box (column 1, below the track metadata). The current line is full brightness;
lines above and below fade out symmetrically:

```
                  line −3   (20% opacity, 10px)
               line −2      (35% opacity, 11px)
            line −1         (65% opacity, 12px)
     ▶  CURRENT LINE        (100% opacity, 14px bold)
            line +1         (65% opacity, 12px)
               line +2      (35% opacity, 11px)
                  line +3   (20% opacity, 10px)
```

The display scrolls automatically — no interaction needed.

## Timing Tiers (best to worst)

| Source | How | Accuracy |
|---|---|---|
| **LRC (syncedLyrics)** | LRCLIB timestamps, stored in `state/lyrics_lrc.json` | Line-accurate |
| **Vocal cue range** | Traktor Cue 3 (Vocal In) + Cue 6 (Vocal Out) distribute lines across the sung portion | Good — locks to actual vocal window |
| **Full-duration estimate** | Lines distributed evenly across total track length | Rough — better than nothing |

Bilby tries them in that order. LRC beats everything.

## Setting Cue Points for Better Sync

If a track does not have LRC data, Bilby uses two Traktor cue points to determine
**when lyrics start and end** so it doesn't scroll during the intro or outro:

| Traktor UI | HOTCUE# | Name | Meaning |
|---|---|---|---|
| **Cue 3** | HOTCUE=2 | Vocal In  | First word of lyrics |
| **Cue 6** | HOTCUE=5 | Vocal Out | Last word of lyrics  |

With both cues set, Bilby distributes all lyric lines evenly across just that window.
The display sits blank during the intro, scrolls during vocals, and stops at the outro.

**To set them in Traktor:**
1. Play the track, navigate to the first vocal
2. Press `3` on the cue strip (or click hotcue slot 3) to drop **Vocal In**
3. Navigate to the last vocal
4. Press `6` to drop **Vocal Out**
5. Save the collection (Traktor auto-saves on exit)

Bilby picks these up at next startup — no restart needed if using the
live lsof/history detection.

## Scrolling Clock

- **With HistoryWatcher position ticks** (fires after a crossfade): lyrics scroll
  in real time from second 0 of the new track.
- **Client-side fallback** (for tracks detected at Bilby startup or mid-song):
  a 200ms browser timer counts wall-clock seconds from detection. Not perfectly
  synced but readable.
- Once real SSE position events arrive they take over and the fallback timer stops.

## Lyrics Source

Lyrics come from `state/lyrics_raw.json` (the `lyrics_plain` field on each Track),
keyed as `artist.lower()\ttitle.lower()`. Parenthetical suffixes like
`(Extended Version)` or `(Cyberpunk Version)` are stripped before lookup so most
variants resolve correctly.

Genius scraping artifacts (`"1 ContributorSong Title Lyrics..."`) are stripped
at render time. Section headers (`[Intro]`, `[Chorus]`, etc.) are dropped — only
sung lines are shown.

## Upgrading to LRC

Run the backfill to fetch timestamped lyrics from LRCLIB for every track that
has them:

```bash
cd "/Users/aaronrhodes/development/music organize"
make backfill-lrc           # all tracks
make backfill-lrc LIMIT=500 # first 500 (test run)
```

Restart Bilby after the backfill. Tracks with LRC data will show the status
indicator `◉ LRC sync`; estimated tracks show `◉ estimated`.
