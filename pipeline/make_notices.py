"""
make_notices.py - generate THIRD-PARTY-NOTICES.txt for the release zip.

    python make_notices.py <output file>

Python libraries are listed from their own package metadata in libs\ (each
package's full licence text also ships inside libs\<name>.dist-info). The
bundled programs (Python, FFmpeg, Deno, fpcalc, yt-dlp) are described below.
"""
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

FFmpeg / ffprobe (pipeline\\bin) - GNU General Public License v3 (this build
    is configured with --enable-gpl --enable-version3). Windows "essentials"
    build by gyan.dev: https://www.gyan.dev/ffmpeg/builds/
    FFmpeg source code: https://ffmpeg.org/download.html#get-sources
    These binaries are unmodified.

Deno (pipeline\\bin\\deno.exe) - MIT License. https://deno.com/
    Used by yt-dlp to solve YouTube's JavaScript challenges.

fpcalc / Chromaprint (pipeline\\bin\\fpcalc.exe) - Chromaprint is MIT
    licensed; the fpcalc binary includes FFmpeg libraries (LGPL 2.1).
    https://acoustid.org/chromaprint

Online services used: MusicBrainz (https://musicbrainz.org/) and AcoustID
(https://acoustid.org/) for song identification, and YouTube for audio.
"""


def license_of(meta):
    lic = (meta.get("License-Expression") or "").strip()
    if not lic:
        lic = (meta.get("License") or "").strip()
        if "\n" in lic or len(lic) > 60:  # some packages paste the whole text here
            lic = ""
    if not lic:
        cls = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
               if c.startswith("License ::")]
        lic = ", ".join(cls)
    return lic or "see licence file in its .dist-info folder"


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "THIRD-PARTY-NOTICES.txt"
    rows = []
    for d in sorted(LIBS.glob("*.dist-info"), key=lambda p: p.name.lower()):
        meta = Parser().parsestr((d / "METADATA").read_text(encoding="utf-8", errors="replace"))
        rows.append(f"{meta['Name']} {meta['Version']} - {license_of(meta)}"
                    + (f"\n    {meta['Home-page']}" if meta.get("Home-page") else ""))
    text = ("Third-party software included in this download\n"
            "==============================================\n\n"
            "This app is a front-end that bundles the following open-source software.\n"
            "Each keeps its own licence; full licence texts are included in the\n"
            "folders named below.\n\n"
            + PROGRAMS +
            "\nPython libraries (pipeline\\libs)\n"
            "--------------------------------\n"
            "Full licence texts: pipeline\\libs\\<package>.dist-info\\\n\n"
            + "\n".join(rows) + "\n")
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out} ({len(rows)} libraries)")


if __name__ == "__main__":
    main()
