# Tidies YouTube-style tags before beets sees them, for every .m4a/.mp3 in a folder.
#
#   python cleantitles.py <folder>
#
# "Caamp - Moonsmoke (Official Audio)" by "Caamp" becomes "Moonsmoke". It also
# drops "(Lyric Video)" and similar, " - Topic"/"VEVO" from channel names,
# featured artists from the artist tag when the title already credits them,
# and the month/day from yt-dlp's 20190726-style dates. It only removes things,
# and real version info like "(Live)" or "(Remix)" is left alone.
import re
import sys
from pathlib import Path

import mutagen

JUNK = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"(?:official\s+)?(?:music\s+|lyrics?\s+)?(?:audio|video|visuali[sz]er)"
    r"|official|lyrics?|hd|hq|4k"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
SEP = r"\s*[-–—:|]\s*"

# "(feat. X)", "[ft. X & Y]", "(featuring X)" or a trailing "feat. X"
FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]"
    r"|\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    re.IGNORECASE,
)


def unfeat_artist(title, artist):
    """If the title says "(feat. X)", take X out of the artist tag.
    "Spotless (feat. The Lumineers)" by "Zach Bryan, The Lumineers" ends up
    under "Zach Bryan", which is how MusicBrainz lists it."""
    m = FEAT.search(title)
    if not m:
        return artist
    names = re.split(r"\s*(?:,|&|\band\b)\s*", (m.group(1) or m.group(2)).strip())
    a = artist
    for name in filter(None, names):
        # Only when it's a separate credit after a comma/&/and.
        a = re.sub(r"\s*(?:,|&|\band\b)\s*" + re.escape(name) + r"(?=\s*(?:,|&|\band\b|$))",
                   "", a, flags=re.IGNORECASE)
    a = a.strip(" ,&")
    return a or artist


def clean_artist(artist):
    a = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE)
    a = re.sub(r"\s*VEVO$", "", a)
    return a.strip() or artist


def clean_title(title, artist):
    t = JUNK.sub("", title).strip()
    # Drop a leading "Artist - ". Also try just the first of several artists.
    names = {artist, re.split(r"\s*(?:,|&|\bfeat\.?|\bft\.?|\bx\b)\s*", artist)[0]}
    for name in sorted((n for n in names if n), key=len, reverse=True):
        m = re.match(re.escape(name) + SEP, t, flags=re.IGNORECASE)
        if m and t[m.end():].strip():
            t = t[m.end():].strip()
            break
    return t or title


def process(path):
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return "no tags"
    title = (f.get("title") or [""])[0]
    artist = (f.get("artist") or [""])[0]
    if not title:
        return "no title"
    new_artist = clean_artist(artist) if artist else artist
    new_title = clean_title(title, new_artist)
    if new_artist:
        new_artist = unfeat_artist(new_title, new_artist)
    date = (f.get("date") or [""])[0]
    new_date = date[:4] if re.fullmatch(r"\d{8}", date) else date
    if (new_title, new_artist, new_date) == (title, artist, date):
        return "ok"
    f["title"] = [new_title]
    if artist:
        f["artist"] = [new_artist]
    if new_date != date:
        f["date"] = [new_date]
    if path.suffix.lower() == ".mp3":
        f.save(v2_version=3)
    else:
        f.save()
    changes = [f'"{title}" -> "{new_title}"'] if new_title != title else []
    if new_artist != artist:
        changes.append(f'artist "{artist}" -> "{new_artist}"')
    if new_date != date:
        changes.append(f"date {date} -> {new_date}")
    return "cleaned " + ", ".join(changes)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "newdownloads"
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in (".m4a", ".mp3"):
            continue
        try:
            result = process(p)
        except Exception as e:
            result = f"skipped ({e})"
        if result != "ok":
            print(f"  title: {result} - {p.name}", flush=True)


if __name__ == "__main__":
    main()
