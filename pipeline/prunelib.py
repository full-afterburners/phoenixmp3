# Removes library entries whose file has been deleted. Without this, beets
# would skip a song you deleted and downloaded again as "already in the library".
# Only the database is changed, no files are touched.
#
# If the output folder is missing (unplugged drive, network share down) or most
# of the library seems gone at once (the folder was moved), it does nothing:
# forgetting everything would lose the run-once flags and redo all the tagging.
#
#   python prunelib.py [run-config.yaml]
import os
import sys

from beets import config
from beets.library import Library

# More missing than this, and more than half the library, looks like the
# files are unreachable rather than deleted by hand.
MAX_SURE = 10


def main():
    config.read()
    if len(sys.argv) > 1:
        config.set_file(sys.argv[1])
    folder = config["directory"].as_filename()
    lib = Library(config["library"].as_filename(), folder)
    if not os.path.isdir(folder):
        print(f"prune skipped (output folder not found: {folder}) - library left as is", flush=True)
        return
    items = list(lib.items())
    gone = [i for i in items if not os.path.exists(os.fsdecode(i.path))]
    if len(gone) > MAX_SURE and len(gone) * 2 > len(items):
        print(f"prune skipped ({len(gone)} of {len(items)} songs not found, the files look "
              f"moved or unreachable) - library left as is", flush=True)
        return
    for item in gone:
        print(f"forgot deleted song: {item.artist} - {item.title}", flush=True)
        item.remove()
    if gone:
        print(f"removed {len(gone)} old library entries", flush=True)


if __name__ == "__main__":
    main()
