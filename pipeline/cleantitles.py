"""
cleantitles.py - tidy YouTube-style titles/artists before beets sees them.

Official-channel uploads come through with titles like
    "Caamp - Moonsmoke (Official Audio)"     artist: "Caamp"
which (a) stops beets from confidently matching MusicBrainz, and (b) looks
bad on the iPod if no match is found. This runs between squareart.py and the
beets import, on every .m4a / .mp3 in the given folder:

    python cleantitles.py <folder>

It only ever removes things, and only these:
  * a leading "<artist> - " from the title (when it repeats the artist tag)
  * video junk in brackets: (Official Audio), [Official Music Video],
    (Lyric Video), (Lyrics), (Visualizer), (Audio), (HD) ...
  * a featured artist from the ARTIST tag when the title already says
    "(feat. X)" - the title keeps the credit (MusicBrainz/iTunes convention)
  * " - Topic" / "VEVO" from the end of the artist (YouTube channel names)
  * the month+day from yt-dlp's "20190726"-style date (beets would read the
    whole thing as the year 20190726), leaving "2019"
If cleaning would leave a title empty, the file is left alone.
Needs mutagen (from libs\\) on PYTHONPATH.
"""
import re
import sys
from pathlib import Path

import mutagen

# Bracketed video/upload junk. Conservative on purpose: real version info like
# "(Remastered)", "(Live)", "(Acoustic)", "(Tiborg Remix)" is never touched.
JUNK = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"(?:official\s+)?(?:music\s+|lyrics?\s+)?(?:audio|video|visuali[sz]er)"
    r"|official|lyrics?|hd|hq|4k"
    r")\s*[\)\]]",
    re.IGNORECASE,
)
SEP = r"\s*[-–—:|]\s*"  # -, en dash, em dash, colon, pipe

# "(feat. X)", "[ft. X & Y]", "(featuring X)" or a trailing "feat. X" in the title.
FEAT = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]"
    r"|\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    re.IGNORECASE,
)


def unfeat_artist(title, artist):
    """Featured artists belong in the title, not the artist (the MusicBrainz /
    iTunes convention). YouTube often lists them in both:
        title "Spotless (feat. The Lumineers)", artist "Zach Bryan, The Lumineers"
    -> artist "Zach Bryan". The title keeps its feat. credit, so nothing is lost,
    beets can match, and the song files under "Zach Bryan" on the iPod instead
    of a separate "Zach Bryan, The Lumineers" artist."""
    m = FEAT.search(title)
    if not m:
        return artist
    names = re.split(r"\s*(?:,|&|\band\b)\s*", (m.group(1) or m.group(2)).strip())
    a = artist
    for name in filter(None, names):
        # Only remove a name that's a separate credit after a separator
        # (", X" / " & X" / " and X"), never part of a band name.
        a = re.sub(r"\s*(?:,|&|\band\b)\s*" + re.escape(name) + r"(?=\s*(?:,|&|\band\b|$))",
                   "", a, flags=re.IGNORECASE)
    a = a.strip(" ,&")
    return a or artist


def clean_artist(artist):
    a = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE)
    a = re.sub(r"\s*VEVO$", "", a)  # case-sensitive: "CaampVEVO", not "Vevo"
    return a.strip() or artist


def clean_title(title, artist):
    t = JUNK.sub("", title).strip()
    # Drop a leading "Artist - " that just repeats the artist tag. Also try
    # the first of several artists ("A, B & C" -> "A").
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
        f.save(v2_version=3)  # iPod Nano wants ID3v2.3
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
        except Exception as e:  # noqa: BLE001 - never block the pipeline
            result = f"skipped ({e})"
        if result != "ok":
            print(f"  title: {result} - {p.name}", flush=True)


if __name__ == "__main__":
    main()
