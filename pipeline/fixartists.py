# Runs after the beets import, so every artist shows up once on the player:
# the artist is the main artist and anyone else goes in the title, like
# "Dial Drunk (feat. Post Malone)" by "Noah Kahan".
#  - Songs matched on MusicBrainz: the first credited artist stays, the others
#    move to the title. MusicBrainz lists bands like "Mumford & Sons" as one
#    artist, so those aren't split.
#  - Songs that kept YouTube's info: YouTube Music lists songwriters as
#    artists too, so only the first name is kept. Uploads with no album often
#    put the artist in the title ("MGMT - Kids"), so that gets split.
#  - "feat. X" in titles becomes "(feat. X)", and the album artist is set to
#    the artist so albums group properly.
#  - The sort names (which players sort by) are set to the artist as shown, so
#    the artist list is plain A-Z instead of "Harris, Calvin" style. This one
#    is checked every run so it follows hand edits too.
# Each song is only changed once, so later hand edits are kept.
#
#   python fixartists.py <run-config.yaml> [--pretend]
import re
import sys
import unicodedata

from beets import config
from beets.library import Library

DONE_FLAG = "credits_checked"
SPLIT_TITLE = re.compile(r"^(.+?)\s+[-–—]\s+(.+)$")
# "(feat. X)", "[ft. X]", or a trailing "feat. X" before any (version) brackets
FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]"
    r"|\s+(?:feat\.?|ft\.?|featuring)\s+([^\(\[]+?)(?=\s*[\(\[]|$)",
    re.IGNORECASE,
)
# A version tag at the end of a title, "(PNAU remix)", "[radio edit]"...
VERSION_TAG = re.compile(r"(?:\s*[\(\[][^\)\]]*\b(?:remix|mix|edit|version|live|remaster(?:ed)?|"
                         r"acoustic|instrumental|demo|mono|stereo)\b[^\)\]]*[\)\]])+\s*$", re.I)


def plain(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def join_names(names):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]


def with_featured(title, extra):
    """Put everyone featured into one "(feat. ...)" where the old one was, or
    before a trailing "(remix)"-style tag, or at the end. Co-artists come
    before guests, and names already in the title (a remixer) aren't repeated."""
    m = FEAT.search(title)
    guests = []
    if m:
        guests = [n for n in re.split(r"\s*(?:,|&|\band\b)\s*", m.group(1) or m.group(2)) if n]
        at = m.start()
        title = title[:m.start()] + title[m.end():]
    else:
        v = VERSION_TAG.search(title)
        at = v.start() if v else len(title)
    shown = plain(title + " " + " ".join(guests))
    names = [n for n in extra if plain(n) not in shown] + guests
    if not names:
        return title
    return f"{title[:at].rstrip()} (feat. {join_names(names)}){title[at:]}".strip()


def credits(item):
    artist, title = item.artist or "", item.title or ""
    if item.mb_trackid:
        names = [n for n in (item.artists or []) if n]
        if len(names) > 1:
            return names[0], with_featured(title, names[1:])
        return artist, with_featured(title, [])
    if not item.album:
        m = SPLIT_TITLE.match(title)
        if m:
            artist, title = m.group(1), m.group(2)
    names = [n.strip() for n in artist.split(",") if n.strip()]
    return (names[0] if names else artist), with_featured(title, [])


def main():
    pretend = "--pretend" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--pretend"]
    config.read()
    if args:
        config.set_file(args[0])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    for item in lib.items():
        if item.get(DONE_FLAG):
            # Players sort by the sort-name tags. MusicBrainz fills them with
            # "Harris, Calvin" style names, so make them match what's shown.
            if not pretend and (item.artist_sort != item.artist
                                or item.albumartist_sort != item.albumartist):
                item.artist_sort, item.albumartist_sort = item.artist, item.albumartist
                if item.try_write():
                    item.store()
                else:
                    print(f"  artist: couldn't write tags - {item.artist} - {item.title}",
                          flush=True)
            continue
        artist, title = credits(item)
        changed = (artist, title) != (item.artist, item.title)
        if changed:
            print(f"  artist: {item.artist} - {item.title}  ->  {artist} - {title}", flush=True)
        if pretend:
            continue
        item.artist, item.title = artist, title
        item.albumartist = artist
        item.artist_sort = item.albumartist_sort = artist
        if not item.try_write():
            # Locked (open in a player, syncing) or read-only. Not stored and
            # not flagged, so the library still matches the file and it's
            # tried again next run.
            print(f"  artist: couldn't write tags, will try again next time - {artist} - {title}",
                  flush=True)
            continue
        if changed:
            try:
                item.move()  # the file name has the artist and title in it
            except Exception as e:
                print(f"  artist: couldn't rename file ({e}) - {artist} - {title}", flush=True)
        item[DONE_FLAG] = 1
        item.store()


if __name__ == "__main__":
    main()
