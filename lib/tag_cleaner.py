"""
Tag and filename artifact cleaning.

Handles common naming problems found in the collection:
  - y2mate.com prefixes
  - YouTube video IDs embedded in filenames
  - Artist names appended to titles after a hyphen
  - Genre prefixes (e.g. "80s - ")
  - Quality/bitrate suffixes (HQ, 320k, etc.)
  - YouTube video title suffixes (OFFICIAL VIDEO, Official Audio, etc.)
  - Placeholder names ("artist - Track N")
  - Double dots and other punctuation artifacts
"""

import re
import os

# 11-char YouTube video ID: base64url chars at end of stem
_YOUTUBE_ID_RE = re.compile(r'[_\-][A-Za-z0-9_\-]{11}$')

# y2mate.com prefix
_Y2MATE_RE = re.compile(r'^y2mate\.com\s*-\s*', re.IGNORECASE)

# YouTube/download suffix in title
_OFFICIAL_SUFFIX_RE = re.compile(
    r'\s*[\(\[](official\s*(video|audio|music\s*video|lyric\s*video|clip)|lyrics?|hq|hd|4k|mv)[\)\]]\s*$',
    re.IGNORECASE
)

# Quality markers anywhere in name: HQ, 320k, 192k, etc.
_QUALITY_RE = re.compile(r'\s*[\-_]?\s*(HQ|HD|4K|320k|256k|192k|128k)\s*$', re.IGNORECASE)

# Double dots
_DOUBLE_DOT_RE = re.compile(r'\.{2,}')

# Track number prefix: "04-", "04 - ", "04. "
_TRACK_NUM_PREFIX_RE = re.compile(r'^\d{1,3}[\s\-\.]+')

# Genre prefix: "80s - ", "Rock - ", "Goth - " etc.
# Match word/phrase followed by " - " at the start
_GENRE_PREFIX_RE = re.compile(r'^[A-Za-z0-9\s&]{1,20}\s+-\s+')

# Placeholder patterns
_PLACEHOLDER_ARTIST_RE = re.compile(r'^artist$', re.IGNORECASE)
_PLACEHOLDER_TITLE_RE = re.compile(r'^(track\s*\d+|unknown|untitled|track)$', re.IGNORECASE)

# Common genre words — used to decide if a prefix is actually a genre label
_GENRE_WORDS = {
    '80s', '70s', '60s', '90s', '00s', 'rock', 'pop', 'goth', 'metal', 'punk',
    'industrial', 'electronic', 'edm', 'techno', 'house', 'trance', 'jazz',
    'blues', 'classical', 'country', 'hip hop', 'hiphop', 'rap', 'r&b', 'rnb',
    'alternative', 'indie', 'folk', 'reggae', 'soul', 'funk', 'disco', 'ambient',
    'darkwave', 'ebm', 'noise', 'drone', 'psytrance', 'psy', 'vaporwave',
}


def clean_stem(stem: str) -> tuple[str | None, str | None]:
    """
    Clean a filename stem (no extension) into (artist, title) or (None, title).

    Returns (artist, title). artist may be None if it couldn't be determined.
    Returns (None, None) if the title is a placeholder.
    """
    s = stem.strip()

    # Strip y2mate prefix
    s = _Y2MATE_RE.sub('', s).strip()

    # Strip YouTube video ID suffix (e.g. _YXH_9707PLc)
    s = _YOUTUBE_ID_RE.sub('', s).strip()

    # Strip official video/audio suffixes
    s = _OFFICIAL_SUFFIX_RE.sub('', s).strip()

    # Strip quality markers
    s = _QUALITY_RE.sub('', s).strip()

    # Strip double dots
    s = _DOUBLE_DOT_RE.sub('.', s).strip()

    # Strip track number prefix (only if no artist detected yet)
    s_no_track = _TRACK_NUM_PREFIX_RE.sub('', s).strip()

    # Replace underscores with spaces (YouTube-ripped filenames)
    # Only if there are multiple underscores (snake_case style)
    if s_no_track.count('_') >= 2:
        s_no_track = s_no_track.replace('_', ' ')

    s = s_no_track

    # Detect "Title- Artist" format (hyphen directly after title, no leading space)
    # before normalizing, so we can preserve the meaning.
    # e.g. "Like A Prayer- Madonna", "HoldMeNow- Thompson Twins"
    _title_first = False
    _title_artist_match = re.match(r'^(.+\S)-\s+([A-Za-z].*)$', s)
    if _title_artist_match:
        _title_first = True

    # Normalize "Title- Artist" and "Title -Artist" → "Title - Artist"
    s = re.sub(r'\s*-\s+', ' - ', s)   # "X- Y" → "X - Y"
    s = re.sub(r'\s+-\s*', ' - ', s)   # "X -Y" → "X - Y"

    # Now try to split "Artist - Title" or "Title - Artist" pattern
    artist = None
    title = s

    if ' - ' in s:
        parts = s.split(' - ', 1)
        left, right = parts[0].strip(), parts[1].strip()

        # Strip quality markers from right side: "The World is Yours - Arch Enemy - HQ - 320k"
        # Multiple " - " splits: take first two and check
        all_parts = [p.strip() for p in s.split(' - ')]
        # Filter out quality/noise tokens from the end
        clean_parts = []
        for p in all_parts:
            if re.match(r'^(HQ|HD|4K|\d+k|official.*|lyrics?.*|audio.*)$', p, re.IGNORECASE):
                break
            clean_parts.append(p)

        if len(clean_parts) >= 2:
            left, right = clean_parts[0], ' - '.join(clean_parts[1:])

        # Check if left side is a genre word → strip it, keep right as title
        if left.lower() in _GENRE_WORDS:
            artist = None
            title = right
        elif _title_first:
            # "Title- Artist" compact format detected pre-normalization
            artist = right
            title = left
        else:
            # Assume "Artist - Title" format (most common)
            artist = left
            title = right

    # Check for "Title-Artist" (no spaces around hyphen, artist at end)
    elif '-' in s and not s.startswith('-'):
        # Check for pattern: "SongName-ArtistName" where ArtistName is capitalized
        m = re.match(r'^(.+?)-([A-Z][A-Za-z\s]+)$', s)
        if m:
            title_part = m.group(1).strip()
            artist_part = m.group(2).strip()
            # Only swap if artist_part is short (likely a real artist name)
            if len(artist_part.split()) <= 4:
                artist = artist_part
                title = title_part

    # Final cleanup
    if title:
        title = title.strip(' -_.,')
    if artist:
        artist = artist.strip(' -_.,')

    # Check for placeholders
    if _PLACEHOLDER_ARTIST_RE.match(artist or ''):
        artist = None
    if _PLACEHOLDER_TITLE_RE.match(title or ''):
        return None, None

    return artist, title if title else None


def clean_existing_tags(existing: dict) -> dict:
    """
    Given a dict with existing tag values {artist, title, album, ...},
    apply cleaning rules and return corrected dict.
    Preserves fields that are already clean.
    """
    result = dict(existing)

    title = existing.get('title', '') or ''
    artist = existing.get('artist', '') or ''

    # If title looks like a filename artifact, try to clean the stem
    needs_clean = (
        _Y2MATE_RE.search(title)
        or _YOUTUBE_ID_RE.search(os.path.splitext(title)[0])
        or _OFFICIAL_SUFFIX_RE.search(title)
        or _PLACEHOLDER_TITLE_RE.match(title)
    )

    if needs_clean or not artist:
        stem = title or ''
        cleaned_artist, cleaned_title = clean_stem(stem)
        if cleaned_title:
            result['title'] = cleaned_title
        if cleaned_artist and not artist:
            result['artist'] = cleaned_artist

    return result


def is_placeholder(artist: str | None, title: str | None) -> bool:
    """Return True if artist/title are placeholders that need manual review."""
    if not title or _PLACEHOLDER_TITLE_RE.match(title):
        return True
    if not artist or _PLACEHOLDER_ARTIST_RE.match(artist):
        return True
    return False
