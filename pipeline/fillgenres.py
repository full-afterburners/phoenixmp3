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
#  - lastgenre.source picks where tags come from: track (the song, then its
#    album, then its artist), album (album, then artist) or artist.
#  - lastgenre.family: sound regroups sub-genres by what their name says
#    ("dream pop" under pop, not rock), and lastgenre.moves puts chosen genres
#    under a different parent ("singer-songwriter: folk"). Both are made with
#    the Genre Picker; leaving them out keeps beets' tree exactly as it is.
#  - Every song's Last.fm tag lists (song, album, artist) are kept in the
#    library (lastfm_* fields), so genres can be re-filed with new settings
#    without asking Last.fm again. New songs come first; songs that already
#    have a genre get theirs kept 100 per run afterwards ("tags:" lines). Lists
#    are fetched again if the song's artist, title or album changes. If Last.fm
#    can't be reached nothing is saved and the song is tried again next run;
#    after 5 failures in a row the step stops and exits with an error.
#  - --refile re-picks the genre of songs that already have one, from the kept
#    tags, using the current genres.txt, source, family, moves and fallbacks.
#    (Last.fm-side filters such as min_weight or an ignorelist were applied when
#    the tags were fetched, so changing those needs fresh lookups.) Genres set
#    by hand are kept: genre_filed remembers the genre this script chose.
#    --since <unix time> limits the run to songs added after that time (one
#    download). --pretend changes nothing.
#  - Your own choices made in the app live in my-genres.txt (the list) and
#    my-genres.yaml (lastgenre source/family/moves). Both are personal, so a new
#    release can't overwrite them, and when present they win over genres.txt and
#    config.yaml. --rules <folder> reads them from another folder instead.
#  - --preview tries the rules on every song that has kept tags, without
#    asking Last.fm or changing anything. It prints a "move:" line (song id,
#    genre now, genre then; tab-separated) for each song whose genre would
#    change, then a "preview:" summary. The app uses it with --rules to count
#    exactly what unsaved changes would do.
#
#   python fillgenres.py [<run-config.yaml>] [--pretend] [--refile] [--since <time>]
#                        [--rules <folder>] [--preview]
import datetime
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pylast
from beets import config
from beets.library import Item, Library

HERE = Path(__file__).resolve().parent
JOINERS = re.compile(r"\s*(?:,|&|\+|/|\bx\b|\bvs\.?|\bwith\b|\bf\./|\bfeat\.?|\bft\.?)\s*", re.IGNORECASE)


MY_LIST = "my-genres.txt"      # personal copies of the genre settings, see the top comment
MY_RULES = "my-genres.yaml"


def list_file(rules: Path = HERE) -> Path:
    """The genre list in use: my-genres.txt in the rules folder if there is one, else genres.txt."""
    mine = rules / MY_LIST
    return mine if mine.exists() else HERE / "genres.txt"


def allowed(rules: Path = HERE):
    lines = list_file(rules).read_text(encoding="utf-8").splitlines()
    return {ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")}


# "Sounds like" grouping: a genre whose name ends in one of these goes under that genre.
SOUND_HEADS = {"pop": "pop", "rock": "rock", "metal": "heavy metal", "punk": "punk rock",
               "folk": "folk", "jazz": "jazz", "blues": "blues", "country": "country", "soul": "soul",
               "funk": "funk", "house": "house", "techno": "techno", "trance": "trance", "disco": "disco",
               "hip hop": "hip hop", "rap": "hip hop", "reggae": "reggae", "ska": "ska", "gospel": "gospel"}


def tree_parents(branches: list[list[str]]) -> dict[str, str | None]:
    """{genre: parent} from beets' flattened tree; the first branch wins, like find_parents."""
    parent: dict[str, str | None] = {}
    for branch in branches:
        for i, genre in enumerate(branch):
            parent.setdefault(genre, branch[i - 1] if i else None)
    return parent


def ancestors(name: str, parent: dict) -> list[str]:
    out = []
    while parent.get(name) is not None and len(out) <= len(parent):
        name = parent[name]
        out.append(name)
    return out


def sound_parents(parent: dict) -> dict[str, str | None]:
    """The tree regrouped by name: "dream pop" under pop, "rap metal" under heavy metal.
    Names that don't end in a known genre keep their place."""
    sound = dict(parent)
    for name in parent:
        words = re.split(r"[ \-]+", name)
        if len(words) < 2:
            continue
        head = SOUND_HEADS.get(" ".join(words[-2:])) or SOUND_HEADS.get(words[-1])
        if not head or head == name or head not in parent or name in ancestors(head, sound):
            continue
        sound[name] = head
    return sound


def regrouped_chains(branches: list[list[str]], family: str, moves: dict) -> dict | None:
    """Each genre's chain of parents (closest first, like beets' find_parents) after
    the family regrouping and the user's moves. None when neither changes anything,
    so beets' own tree is used untouched. Unchanged parts follow beets' tree exactly."""
    import beetsplug.lastgenre as lg_module
    # beets' own lookup, even if an earlier make_plugin() already pointed it at regrouped chains
    find_parents = getattr(lg_module, "_beets_find_parents", lg_module.find_parents)
    parent = tree_parents(branches)
    changed: dict[str, str | None] = {}
    if family == "sound":
        changed = {n: p for n, p in sound_parents(parent).items() if p != parent[n]}

    def chain(name: str, depth: int = 0) -> list[str]:
        if depth > len(parent):
            raise ValueError("genre moves loop")
        if name in changed:
            up = changed[name]
            return [name, *(chain(up, depth + 1) if up else [])]
        orig = find_parents(name, branches)
        for i in range(1, len(orig)):
            if orig[i] in changed:
                return orig[:i] + chain(orig[i], depth + 1)
        return orig

    for name, to in moves.items():
        name, to = str(name).lower(), str(to or "top").lower()
        if name not in parent or (to != "top" and to not in parent):
            print(f"  genre: couldn't move {name} under {to} (not in the genre tree)", flush=True)
            continue
        if to != "top" and name in chain(to):
            print(f"  genre: couldn't move {name} under {to} (it's one of its own sub-genres)", flush=True)
            continue
        changed[name] = None if to == "top" else to
    if not changed:
        return None
    return {name: chain(name) for name in parent}


def use_chains(chains: dict | None) -> None:
    """Point the lastgenre plugin's tree lookups at the regrouped chains (or back at beets' tree)."""
    import beetsplug.lastgenre as lg_module
    original = getattr(lg_module, "_beets_find_parents", lg_module.find_parents)
    lg_module._beets_find_parents = original
    if chains is None:
        lg_module.find_parents = original
    else:
        lg_module.find_parents = lambda candidate, branches: list(chains.get(candidate.lower()) or [candidate])


def make_plugin(rules: Path = HERE):
    """beets' lastgenre plugin, set up so the genre tree decides first and the
    lastgenre.fallbacks patterns in config.yaml only cover tags it doesn't know.
    lastgenre.family and lastgenre.moves regroup the tree first."""
    lg = config["lastgenre"]
    lg["whitelist"] = str(list_file(rules))
    lg["force"] = True
    lg["keep_existing"] = False
    lg["aliases"] = True  # beets' built-in spelling fixes, not a custom list
    fallbacks = [(re.compile(p), genre)
                 for genre, pats in (lg["fallbacks"].get() or {}).items() for p in pats]
    from beetsplug.lastgenre import LastGenrePlugin
    plugin = LastGenrePlugin()
    family = lg["family"].as_choice(["history", "sound"]) if lg["family"].exists() else "history"
    moves = (lg["moves"].get(dict) or {}) if lg["moves"].exists() else {}
    use_chains(regrouped_chains(plugin.c14n_branches, family, moves))
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


class LookupFailed(Exception):
    """Last.fm couldn't be asked (offline, blocked, server trouble), as opposed to
    Last.fm answering that it has no tags."""


# Where each Last.fm tag list is kept on the song in the library (beets flexible
# attributes, stored as JSON lists). "1" = looked up with just the first artist.
TAG_FIELDS = {"track": "lastfm_track", "album": "lastfm_album", "artist": "lastfm_artist",
              "track1": "lastfm_track1", "artist1": "lastfm_artist1"}
FETCHED_FIELD = "lastfm_fetched"   # date the lists were fetched
KEY_FIELD = "lastfm_key"           # artist/title/album they were fetched for
FILED_FIELD = "genre_filed"        # the genre this script chose, to tell it from hand-set ones
BACKFILL_PER_RUN = 100             # already-sorted songs whose tags are kept per run
MAX_FAILS = 5                      # lookups failing in a row before giving up (offline)


def first_artist(artist: str) -> str | None:
    """"A feat. B" -> "A", or None when there's only one artist."""
    first = JOINERS.split(artist or "")[0].strip()
    return first if first and first != artist else None


def tag_key(item) -> str:
    return json.dumps([item.artist, item.albumartist, item.title, item.album], ensure_ascii=False)


def stored_tags(item) -> dict | None:
    """The kept tag lists, or None if there are none or they were fetched for a
    different artist/title/album (the song was renamed since)."""
    if FETCHED_FIELD not in item or item.get(KEY_FIELD) != tag_key(item):
        return None
    return {kind: json.loads(item[field]) for kind, field in TAG_FIELDS.items() if field in item}


def store_tags(item, tags: dict) -> None:
    for kind, field in TAG_FIELDS.items():
        if kind in tags:
            item[field] = json.dumps(tags[kind], ensure_ascii=False)
        elif field in item:
            del item[field]
    item[FETCHED_FIELD] = datetime.date.today().isoformat()
    item[KEY_FIELD] = tag_key(item)


def hand_set(item, current: list[str]) -> bool:
    """A genre this script didn't choose: different from the one it wrote, or,
    for songs filed before it kept track, more than one genre."""
    filed = item.get(FILED_FIELD)
    if filed is not None:
        return current != [filed]
    return len(current) > 1


class GenreLookup:
    """Fetches every Last.fm tag list a song's genre can come from (its own, its
    album's, its artist's, and the first artist's when there are several), then
    picks the genre from those lists exactly the way beets' lastgenre would, so
    a song can be re-filed later without asking Last.fm again."""

    def __init__(self, plugin):
        from beetsplug.lastgenre import client as lg_client
        lg_client.LASTFM.enable_rate_limit()   # pylast spaces requests out (Last.fm allows ~5 a second)
        self.plugin = plugin
        self.client = plugin.client
        self.real_fetch = plugin.client.fetch
        self.cache: dict[tuple, list[str]] = {}
        self.serving: dict = {}
        self.fails = 0
        self.client.fetch_genres = self._fetch_genres_strict
        self.client.fetch = self._serve

    def _fetch_genres_strict(self, entity) -> list[str]:
        # beets' own version turns every error into "no tags"; only "not found" is that
        try:
            res = entity.get_top_tags()
        except pylast.WSError as e:
            if str(e.status) in ("6", "7"):   # Last.fm doesn't know this song/album/artist
                return []
            raise LookupFailed(f"Last.fm: {e}") from e
        except (pylast.NetworkError, pylast.MalformedResponseError) as e:
            raise LookupFailed(f"Last.fm: {e}") from e
        if self.client.min_weight:
            res = [el for el in res if int(el.weight or 0) >= self.client.min_weight]
        return [el.item.get_name().lower() for el in res]

    def _get(self, kind: str, item) -> list[str]:
        key = (kind, *self.client.FETCH_METHODS[kind][1](item))
        if key not in self.cache:   # albums and artists repeat a lot within one run
            self.cache[key] = self.real_fetch(kind, item)
        return self.cache[key]

    def fetch_all(self, item) -> dict:
        tags = {kind: self._get(kind, item) for kind in ("track", "album", "artist")}
        first = first_artist(item.artist)
        if first:
            solo = Item(artist=first, albumartist=item.albumartist, title=item.title, album=item.album)
            tags["track1"] = self._get("track", solo)
            tags["artist1"] = self._get("artist", solo)
        return tags

    def tags_for(self, item, pretend: bool) -> dict | None:
        """Kept tags, or freshly fetched (and kept) ones; None if Last.fm couldn't
        be reached. Stops the whole step after MAX_FAILS failures in a row."""
        tags = stored_tags(item)
        if tags is not None:
            return tags
        try:
            tags = self.fetch_all(item)
        except LookupFailed as e:
            self.fails += 1
            print(f"  genre: lookup failed ({e}) - {item.artist} - {item.title}", flush=True)
            if self.fails >= MAX_FAILS:
                print("  genre: couldn't reach Last.fm, stopping (songs will be tried again next run)", flush=True)
                sys.exit(1)
            return None
        self.fails = 0
        if not pretend:
            store_tags(item, tags)
            item.store()   # library only: the tag lists aren't written into the file
        return tags

    def _serve(self, kind: str, obj, *args) -> list[str]:
        solo = self.serving["solo"] and kind in ("track", "artist")
        return self.serving["tags"].get(kind + ("1" if solo else ""), [])

    def genre(self, item, tags: dict, ok: set) -> list[str]:
        """The listed genre beets picks from these tag lists, or []."""
        self.serving = {"tags": tags, "solo": False}
        genres, _ = self.plugin._get_genre(item)
        first = first_artist(item.artist)
        if not genres and first and "track1" in tags:
            self.serving["solo"] = True
            real = item.artist
            item.artist = first
            try:
                genres, _ = self.plugin._get_genre(item)
            finally:
                item.artist = real
        return [g for g in genres if g.lower() in ok][:1]


@dataclass(frozen=True)
class Args:
    run_config: str | None = None
    pretend: bool = False
    refile: bool = False
    since: float | None = None
    rules: Path = HERE
    preview: bool = False


def parse_args(argv: list[str]) -> Args:
    opts, rest = {}, []
    it = iter(argv)
    for a in it:
        if a in ("--pretend", "--refile", "--preview"):
            opts[a[2:]] = True
        elif a == "--since":
            opts["since"] = float(next(it))
        elif a == "--rules":
            opts["rules"] = Path(next(it))
        else:
            rest.append(a)
    if opts.get("preview"):   # a preview is a re-file that changes nothing
        opts.update(refile=True, pretend=True)
    return Args(run_config=rest[0] if rest else None, **opts)


def file_genre(plugin, lookup: GenreLookup, item, tags: dict, current: list[str], ok: set, pretend: bool) -> list[str]:
    """Pick the song's genre from its tags, report it, and write it to the file. Returns the genres picked."""
    name = f"{item.artist} - {item.title}"
    genres = lookup.genre(item, tags, ok)
    found = bool(genres)
    if found:
        print(f"  genre: {name}: {genres[0]}", flush=True)
    else:
        genres = from_current(plugin, item, current, ok)
        note = f"none found, kept {genres[0]}" if genres else "none found"
        if genres and genres != current[:1]:
            note = f"none found, {current[0]} -> {genres[0]}"
        print(f"  genre: {name}: {note}", flush=True)
    if pretend:
        return genres
    if genres == current:
        if found and item.get(FILED_FIELD) != genres[0]:
            item[FILED_FIELD] = genres[0]   # the same genre, now known to be ours
            item.store()
        return genres
    item.genres = genres
    if genres:
        item[FILED_FIELD] = genres[0]
    if item.try_write():
        item.store()
    else:
        print(f"  genre: couldn't write tags, will try again next time - {name}", flush=True)
    return genres


def keep_tags(lookup: GenreLookup, items: list) -> None:
    """Fetch and keep tags for songs that already have a genre, a batch per run."""
    batch = items[:BACKFILL_PER_RUN]
    for n, item in enumerate(batch, 1):
        lookup.tags_for(item, pretend=False)
        if n % 25 == 0 or n == len(batch):
            print(f"  tags: kept Last.fm tags for {n} of {len(items)} sorted songs", flush=True)


def main():
    args = parse_args(sys.argv[1:])
    config.read()
    if args.run_config:
        config.set_file(args.run_config)
    if (args.rules / MY_RULES).exists():
        config.set_file(str(args.rules / MY_RULES))   # your own source/family/moves win
    plugin = make_plugin(args.rules)
    lookup = GenreLookup(plugin)
    ok = allowed(args.rules)

    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    backfill, changes, checked, untagged = [], 0, 0, 0
    for item in lib.items():
        if args.since is not None and item.added < args.since:
            continue
        current = list(item.get("genres", with_album=False) or [])
        listed = bool(current) and current[0].lower() in ok
        if listed and not args.refile:
            if stored_tags(item) is None:
                backfill.append(item)   # done after the new songs, so they're tagged first
            continue
        if listed and hand_set(item, current):
            print(f"  genre: {item.artist} - {item.title}: kept hand-set {', '.join(current)}", flush=True)
            continue
        if args.preview:   # kept tags only: no Last.fm, so it's quick and works offline
            tags = stored_tags(item)
            if tags is None:
                untagged += 1
                continue
            checked += 1
            picked = file_genre(plugin, lookup, item, tags, current, ok, pretend=True)
            if picked[:1] != current[:1]:
                changes += 1
                print(f"  move: {item.id}\t{current[0] if current else ''}\t{picked[0] if picked else ''}", flush=True)
            continue
        tags = lookup.tags_for(item, args.pretend)
        if tags is not None:
            file_genre(plugin, lookup, item, tags, current, ok, args.pretend)
    if args.preview:
        print(f"preview: {changes} would change, {checked} checked, {untagged} without kept tags", flush=True)
    if backfill and not args.pretend:
        keep_tags(lookup, backfill)


if __name__ == "__main__":
    main()
