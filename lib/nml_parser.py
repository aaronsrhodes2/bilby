"""
Traktor NML path encoding/decoding utilities.

Traktor uses a colon-separated path format:
  DIR:  /:Users/:aaronrhodes/:Music/:Artist/:Album/:
  FILE: Track Name.mp3
  VOLUME: Macintosh HD

PRIMARYKEY (in playlist NMLs):
  Macintosh HD/:Users/:aaronrhodes/:Music/:Artist/:Album/:Track Name.mp3
"""

VOLUME = "Macintosh HD"


def traktor_to_abs(volume: str, dir_str: str, filename: str) -> str:
    """Convert Traktor LOCATION fields to an absolute POSIX path."""
    # dir_str looks like: /:Users/:aaronrhodes/:Music/:Foo/:Bar/:
    # Strip leading /: and trailing /:
    stripped = dir_str.strip()
    if stripped.startswith("/:"):
        stripped = stripped[2:]
    if stripped.endswith("/:"):
        stripped = stripped[:-2]
    # Split on /: to get path components
    if stripped:
        parts = stripped.split("/:")
    else:
        parts = []
    return "/" + "/".join(parts) + "/" + filename if parts else "/" + filename


def abs_to_traktor_location(abs_path: str) -> dict:
    """Convert an absolute POSIX path to Traktor LOCATION element attributes."""
    # abs_path: /Users/aaronrhodes/development/music organize/corrected_music/Artist/Album/Track.mp3
    parts = abs_path.split("/")
    filename = parts[-1]
    dir_parts = parts[1:-1]  # skip leading '' and filename
    if dir_parts:
        dir_str = "/:" + "/:" .join(dir_parts) + "/:"
    else:
        dir_str = "/:"
    return {
        "DIR": dir_str,
        "FILE": filename,
        "VOLUME": VOLUME,
        "VOLUMEID": VOLUME,
    }


def primarykey_to_abs(primarykey: str) -> str:
    """Convert a Traktor PRIMARYKEY string to an absolute POSIX path.

    Format: 'Macintosh HD/:Users/:aaronrhodes/:Music/:path/:to/:file.mp3'
    """
    # Strip the volume prefix
    pk = primarykey
    if pk.startswith(VOLUME):
        pk = pk[len(VOLUME):]
    # Now looks like: /:Users/:aaronrhodes/:.../:file.mp3
    # Split on /: — first element will be empty
    parts = pk.split("/:")
    # parts[0] is '' (before first /:), rest are path components
    components = [p for p in parts if p]
    return "/" + "/".join(components)


def abs_to_primarykey(abs_path: str) -> str:
    """Convert an absolute POSIX path to a Traktor PRIMARYKEY string."""
    parts = abs_path.split("/")
    components = [p for p in parts if p]
    return VOLUME + "/:" + "/:" .join(components)
