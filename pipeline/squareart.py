# Crops the embedded cover art of every .m4a/.mp3 in a folder to a centred
# square and shrinks it. YouTube thumbnails are 16:9 and players show art square.
#
#   python squareart.py <folder> [size]     (size defaults to 500 px)
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

SIZE = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 500
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HERE = Path(__file__).resolve().parent
FFMPEG = shutil.which("ffmpeg") or str(HERE / "bin" / "ffmpeg.exe")

# Crop to the shorter side, then scale down (never up).
VF = f"crop='min(iw,ih)':'min(iw,ih)',scale='min({SIZE},iw)':'min({SIZE},ih)'"


def jpeg_size(data):
    """Width and height of a JPEG, read from its header. None if not a JPEG."""
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


def process_mp3(path):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return "no tags"
    frames = tags.getall("APIC")
    if not frames:
        return "no art"
    changed = False
    for f in frames:
        if not needs_fix(f.data):
            continue
        new = fix_image(f.data)
        tags.delall(f"APIC:{f.desc}")
        tags.add(APIC(encoding=f.encoding, mime="image/jpeg", type=f.type, desc=f.desc, data=new))
        changed = True
    if not changed:
        return "ok already"
    tags.save(path, v2_version=3)  # older iPods need ID3v2.3
    return "squared"


def process(path):
    return process_m4a(path) if path.suffix.lower() == ".m4a" else process_mp3(path)


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "newdownloads"
    for f in sorted(p for p in folder.iterdir() if p.suffix.lower() in (".m4a", ".mp3")):
        try:
            result = process(f)
        except Exception as e:
            result = f"skipped ({e})"
        print(f"  art: {result} - {f.name}", flush=True)


if __name__ == "__main__":
    main()
