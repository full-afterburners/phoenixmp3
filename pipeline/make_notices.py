# Writes THIRD-PARTY-NOTICES.txt for the release zip. The Python libraries are
# read from their own metadata in libs\.
#
#   python make_notices.py <output file>
import sys
from email.parser import Parser
from pathlib import Path

HERE = Path(__file__).resolve().parent
LIBS = HERE / "libs"

PROGRAMS = """\
Bundled programs
----------------
Python 3.11.9 (pipeline\\python) - Python Software Foundation License.
    Full text: pipeline\\python\\LICENSE.txt. https://www.python.org/

yt-dlp (pipeline\\yt-dlp-master) - The Unlicense (public domain).
    https://github.com/yt-dlp/yt-dlp

FFmpeg / ffprobe (pipeline\\bin) - GNU General Public License v3 (built with
    --enable-gpl --enable-version3). Windows "essentials" build by gyan.dev:
    https://www.gyan.dev/ffmpeg/builds/
    FFmpeg source code: https://ffmpeg.org/download.html#get-sources
    The binaries are unmodified.

Deno (pipeline\\bin\\deno.exe) - MIT License. https://deno.com/

fpcalc / Chromaprint (pipeline\\bin\\fpcalc.exe) - Chromaprint is MIT licensed,
    the fpcalc binary includes FFmpeg libraries (LGPL 2.1).
    https://acoustid.org/chromaprint

Inter typeface (pipeline\\ui\\fonts) - SIL Open Font License 1.1.
    Full text: pipeline\\ui\\fonts\\OFL.txt. https://github.com/rsms/inter
    (files from the @fontsource-variable/inter package)

proxy_tools 0.1.0 (pipeline\\libs\\proxy_tools, used by pywebview) - BSD
    licence, adapted from Werkzeug. https://github.com/jtushman/proxy_tools

Online services used: MusicBrainz (https://musicbrainz.org/), AcoustID
(https://acoustid.org/), YouTube, Last.fm (https://www.last.fm/), the Cover
Art Archive (https://coverartarchive.org/) and Wikidata / Wikimedia Commons
(artist photos).
"""


def license_of(meta):
    lic = (meta.get("License-Expression") or "").strip()
    if not lic:
        lic = (meta.get("License") or "").strip()
        if "\n" in lic or len(lic) > 60:  # some packages put the whole licence here
            lic = ""
    if not lic:
        cls = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
               if c.startswith("License ::")]
        lic = ", ".join(cls)
    return lic or "see the licence file in its .dist-info folder"


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "THIRD-PARTY-NOTICES.txt"
    rows = []
    for d in sorted(LIBS.glob("*.dist-info"), key=lambda p: p.name.lower()):
        meta = Parser().parsestr((d / "METADATA").read_text(encoding="utf-8", errors="replace"))
        rows.append(f"{meta['Name']} {meta['Version']} - {license_of(meta)}"
                    + (f"\n    {meta['Home-page']}" if meta.get("Home-page") else ""))
    text = ("Third-party software included in this download\n"
            "==============================================\n\n"
            "phoenixmp3 bundles the open source software below. Each keeps its own\n"
            "licence, and the full licence texts are in the folders listed.\n\n"
            + PROGRAMS +
            "\nPython libraries (pipeline\\libs)\n"
            "--------------------------------\n"
            "Full licence texts: pipeline\\libs\\<package>.dist-info\\\n\n"
            + "\n".join(rows) + "\n")
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(rows)} libraries)")


if __name__ == "__main__":
    main()
