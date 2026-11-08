"""
squareart.py - crop embedded cover art to a centred square and shrink it.

YouTube thumbnails are 16:9 (usually 1280x720); the iPod shows album art as a
square, so un-cropped art gets letterboxed or squashed. This runs between the
yt-dlp download and the beets import, on every .m4a and .mp3 in the given folder:

    python squareart.py <folder> [size, default 500]

For each file with embedded art (MP4 "covr" atom or ID3 APIC frame) it pipes the image through
ffmpeg: centre-crop to a square, scale down to at most SIZE px, re-encode as
JPEG. Art that is already square and small enough is left untouched.
Needs mutagen (from libs\\) on PYTHONPATH and ffmpeg on PATH or in ..\\bin.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

SIZE = 500  # px; plenty for any iPod screen, ~30-50 KB per cover
# (the app passes 300 as a 2nd argument for the "smaller cover art" setting)
if len(sys.argv) > 2 and sys.argv[2].isdigit():
    SIZE = int(sys.argv[2])
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HERE = Path(__file__).resolve().parent
FFMPEG = shutil.which("ffmpeg") or str(HERE / "bin" / "ffmpeg.exe")

# Centre-crop to the shorter side, then scale down (never up) to SIZE.
VF = f"crop='min(iw,ih)':'min(iw,ih)',scale='min({SIZE},iw)':'min({SIZE},ih)'"


def jpeg_size(data):
    """Return (w, h) of a JPEG from its SOF marker, or None."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + seglen
    return None


def fix_image(data):
    p = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-vf", VF, "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "3",
         "-f", "image2pipe", "pipe:1"],
        input=data, capture_output=True, creationflags=NO_WINDOW,
    )
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    return p.stdout


def needs_fix(data):
    size = jpeg_size(data)
    return not (size and size[0] == size[1] and size[0] <= SIZE)


def process_m4a(path):
    """AAC/.m4a: cover art lives in the MP4 'covr' atom (a list of images)."""
    mp4 = MP4(path)
    covers = (mp4.tags or {}).get("covr") or []
    if not covers:
        return "no art"
    if not any(needs_fix(bytes(c)) for c in covers):
        return "ok already"
    mp4.tags["covr"] = [
        MP4Cover(fix_image(bytes(c)) if needs_fix(bytes(c)) else bytes(c),
                 imageformat=MP4Cover.FORMAT_JPEG)
        for c in covers
    ]
    mp4.save()
    return "squared"


def process(path):
    if path.suffix.lower() == ".m4a":
        return process_m4a(path)
    mp3 = path
    try:
        tags = ID3(mp3)
    except ID3NoHeaderError:
        return "no tags"
    frames = tags.getall("APIC")
    if not frames:
        return "no art"
    changed = False
    for f in frames:
        if not needs_fix(f.data):
            continue  # already square and small
        new = fix_image(f.data)
        tags.delall(f"APIC:{f.desc}")
        tags.add(APIC(encoding=f.encoding, mime="image/jpeg", type=f.type,
                      desc=f.desc, data=new))
        changed = True
    if not changed:
        return "ok already"
    tags.save(mp3, v2_version=3)  # iPod Nano wants ID3v2.3
    return "squared"


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "newdownloads"
    files = sorted(p for p in folder.iterdir()
                   if p.suffix.lower() in (".m4a", ".mp3"))
    for f in files:
        try:
            result = process(f)
        except Exception as e:  # noqa: BLE001 - never block the pipeline
            result = f"skipped ({e})"
        print(f"  art: {result} - {f.name}", flush=True)


if __name__ == "__main__":
    main()
