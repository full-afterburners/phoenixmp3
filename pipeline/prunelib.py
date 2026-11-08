# Removes library entries whose file has been deleted. Without this, beets
# would skip a song you deleted and downloaded again as "already in the library".
# Only the database is changed, no files are touched.
#
#   python prunelib.py [run-config.yaml]
import os
import sys

from beets import config
from beets.library import Library


def main():
    config.read()
    if len(sys.argv) > 1:
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    gone = [i for i in lib.items() if not os.path.exists(os.fsdecode(i.path))]
    for item in gone:
        print(f"forgot deleted song: {item.artist} - {item.title}", flush=True)
        item.remove()
    if gone:
        print(f"removed {len(gone)} old library entries", flush=True)


if __name__ == "__main__":
    main()
