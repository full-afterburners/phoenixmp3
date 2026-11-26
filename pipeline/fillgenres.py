# Gives each song one broad genre (rock, jazz, folk...) from Last.fm, using
# beets' lastgenre plugin and the list in genres.txt. Runs after fixartists.py.
#  - Songs whose genre isn't on the list (YouTube's "Music", or none) get
#    looked up. Songs that already have a listed genre are left alone.
#  - Last.fm doesn't know combined names like "Ian Asher & Olly Alexander", so
#    if nothing is found it tries again with just the first artist.
#  - If Last.fm finds nothing, a genre set some other way (by hand, say
#    "Shoegaze") is turned into its broad genre ("Rock") the same way. One
#    that can't be placed is kept as it is, never erased. Only YouTube's
#    "Music" is blanked.
#  - Tags are mapped to the list with beets' genre tree first. The patterns in
#    lastgenre.fallbacks in config.yaml only cover tags the tree doesn't know.
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


def make_plugin():
    """beets' lastgenre plugin, set up so the genre tree decides first and the
    lastgenre.fallbacks patterns in config.yaml only cover tags it doesn't know."""
    lg = config["lastgenre"]
    lg["whitelist"] = str(HERE / "genres.txt")
    lg["force"] = True
    lg["keep_existing"] = False
    lg["aliases"] = True  # beets' built-in spelling fixes, not a custom list
    fallbacks = [(re.compile(p), genre)
                 for genre, pats in (lg["fallbacks"].get() or {}).items() for p in pats]
    from beetsplug.lastgenre import LastGenrePlugin
    plugin = LastGenrePlugin()
    tree_only = plugin._resolve_genres

    def resolve(tags, artist=None):
        found = tree_only(tags, artist=artist)
        if found or not tags:
            return found
        mapped = [next((g for pat, g in fallbacks if pat.fullmatch(t.lower())), t) for t in tags]
        return tree_only(mapped, artist=artist) if mapped != tags else []

    plugin._resolve_genres = resolve
    return plugin


def from_current(plugin, item, current, ok):
    """The genre to use when Last.fm found nothing: the current genre mapped to
    a broad one, the current genre unchanged if it can't be mapped, or nothing
    if all it had was YouTube's "Music"."""
    tags = [g.lower() for g in current if g.lower() != "music"]
    if not tags:
        return []
    mapped = plugin._format_genres(plugin._resolve_genres(tags, artist=item.artist))
    mapped = [g for g in mapped if g.lower() in ok][:1]
    return mapped or [g for g in current if g.lower() != "music"][:1]


def main():
    pretend = "--pretend" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--pretend"]
    config.read()
    if args:
        config.set_file(args[0])
    plugin = make_plugin()
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
        if genres:
            print(f"  genre: {name}: {genres[0]}", flush=True)
        else:
            genres = from_current(plugin, item, current, ok)
            note = f"none found, kept {genres[0]}" if genres else "none found"
            if genres and genres != current[:1]:
                note = f"none found, {current[0]} -> {genres[0]}"
            print(f"  genre: {name}: {note}", flush=True)
        if pretend or genres == current:
            continue
        item.genres = genres
        if item.try_write():
            item.store()
        else:
            print(f"  genre: couldn't write tags, will try again next time - {name}", flush=True)


if __name__ == "__main__":
    main()
