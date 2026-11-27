# Where things live in the install, and keeping its top folder tidy. Opening the
# install folder should show just phoenixmp3.exe and music_library: everything else
# (the app, settings, playlists.txt, the read me) is in the pipeline folder, which
# tidy() hides the way Windows hides its own system folders.
#
# Older versions put songs in "downloadswithmeta" and playlists.txt next to the exe.
# tidy() renames/moves those once. The library stores each song's path relative to
# the music folder and the .m3u8 files point at "../<song>", so nothing else needs
# changing. If a move can't happen (a song is open in a player), the old place keeps
# being used and the move is tried again on the next start.
# Both windows call tidy() when they open; everything else just asks for the paths.
import ctypes
import os
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
TOP = PIPELINE.parent
FINAL = TOP / "music_library"            # the default music folder
OLD_FINAL = TOP / "downloadswithmeta"    # its name before
PLAYLISTS = PIPELINE / "playlists.txt"
OLD_PLAYLISTS = TOP / "playlists.txt"
FILE_ATTRIBUTE_HIDDEN = 0x2
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def default_dir() -> Path:
    """The default music folder: music_library, or the old folder while it couldn't be renamed yet."""
    return FINAL if FINAL.exists() or not OLD_FINAL.exists() else OLD_FINAL


def playlists_file() -> Path:
    return PLAYLISTS if PLAYLISTS.exists() or not OLD_PLAYLISTS.exists() else OLD_PLAYLISTS


def _move(old: Path, new: Path) -> str | None:
    if not old.exists() or new.exists():
        return None
    try:
        os.rename(old, new)
        return None
    except OSError as e:
        return f"couldn't move {old.name} to {new.relative_to(TOP)} ({e.strerror or e}); trying again next start"


def _hide(folder: Path) -> str | None:
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        attrs = k32.GetFileAttributesW(str(folder))
        if attrs == INVALID_FILE_ATTRIBUTES or attrs & FILE_ATTRIBUTE_HIDDEN:
            return None
        if not k32.SetFileAttributesW(str(folder), attrs | FILE_ATTRIBUTE_HIDDEN):
            return f"couldn't hide the {folder.name} folder"
    except (AttributeError, OSError) as e:
        return f"couldn't hide the {folder.name} folder ({e})"
    return None


def tidy() -> list[str]:
    """Move old-style files into place, make sure music_library exists, hide pipeline.
    Returns what couldn't be done (for the log); never raises, the app opens either way."""
    problems = [_move(OLD_FINAL, FINAL), _move(OLD_PLAYLISTS, PLAYLISTS)]
    if default_dir() == FINAL:
        try:
            FINAL.mkdir(exist_ok=True)   # there from the first start, so it's obvious where songs go
        except OSError as e:
            problems.append(f"couldn't make the music_library folder ({e.strerror or e})")
    problems.append(_hide(PIPELINE))
    return [p for p in problems if p]
