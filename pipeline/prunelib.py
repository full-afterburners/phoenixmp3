"""
prunelib.py - forget library entries whose song file no longer exists.

    python prunelib.py <run-config.yaml>

beets remembers every song it has imported in library.db. If you delete a song
from the output folder by hand, that memory stays, and the next time you
download the same song beets says "already in the library" and skips it, so it
never reappears. Running this right before each import removes those stale
entries (it only touches the database; it never deletes files).
Needs BEETSDIR set and libs\\ on PYTHONPATH (the app does both).
"""
import os
import sys

from beets import config
from beets.library import Library


def main():
    config.read()
    if len(sys.argv) > 1:  # the app's settings overlay (output folder)
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    gone = [i for i in lib.items() if not os.path.exists(os.fsdecode(i.path))]
    for item in gone:
        print(f"forgot deleted song: {item.artist} - {item.title}", flush=True)
        item.remove()  # database only; the file is already gone
    if gone:
        print(f"removed {len(gone)} stale library entr{'y' if len(gone) == 1 else 'ies'}",
              flush=True)


if __name__ == "__main__":
    main()
