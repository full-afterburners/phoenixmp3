# Gives each song one broad genre (rock, jazz, folk...) from Last.fm, using
# beets' lastgenre plugin and the list in genres.txt. Runs after fixartists.py.
#  - Songs whose genre isn't on the list (YouTube's "Music", or none) get
#    looked up. Songs that already have a listed genre are left alone.
#  - Last.fm doesn't know combined names like "Ian Asher & Olly Alexander", so
#    if nothing is found it tries again with just the first artist.
#  - If nothing is found at all the genre is left empty rather than "Music".
#
#   python fillgenres.py <run-config.yaml> [--pretend]
import re
import sys
from pathlib import Path

from beets import config
from beets.library import Library

HERE = Path(__file__).resolve().parent
JOINERS = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\bvs\.?|\bwith\b|\bf\./|\bfeat\.?|\bft\.?)\s*", re.IGNORECASE)


def allowed():
    lines = (HERE / "genres.txt").read_text(encoding="utf-8").splitlines()
    return {ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")}


def main():
    pretend = "--pretend" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--pretend"]
    config.read()
    if args:
        config.set_file(args[0])
    config["lastgenre"]["whitelist"] = str(HERE / "genres.txt")
    config["lastgenre"]["force"] = True
    config["lastgenre"]["keep_existing"] = False
    from beetsplug.lastgenre import LastGenrePlugin
    plugin = LastGenrePlugin()
    ok = allowed()

    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    for item in lib.items():
        current = list(item.get("genres", with_album=False) or [])
        if current and current[0].lower() in ok:
            continue
        name = f"{item.artist} - {item.title}"
        try:
            genres, _ = plugin._get_genre(item)
            first = JOINERS.split(item.artist or "")[0].strip()
            if not genres and first and first != item.artist:
                real = item.artist
                item.artist = first
                try:
                    genres, _ = plugin._get_genre(item)
                finally:
                    item.artist = real
        except Exception as e:
            print(f"  genre: lookup failed ({e}) - {name}", flush=True)
            continue
        genres = [g for g in genres if g.lower() in ok][:1]
        print(f"  genre: {name}: {genres[0] if genres else 'none found'}", flush=True)
        if pretend or genres == current:
            continue
        item.genres = genres
        item.try_write()
        item.store()


if __name__ == "__main__":
    main()
