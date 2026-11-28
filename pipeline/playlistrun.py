# The My Playlists parts of a download (downloadrun.py runs them), ported from the old
# tkinter window's Update My Playlists:
#  1. list each saved playlist with yt-dlp; songs not in downloaded.txt or
#     unavailable.txt are new.
#  2. songs YouTube won't serve (unavailable.txt) get findcopy.py's search for another
#     upload of the same song (for age-restricted ones, also someone else's exact
#     re-upload); what it finds or doesn't goes in replaced.txt, and
#     errors aren't written so they're tried again next time.
#  3. the playlists themselves are downloaded (yt-dlp skips what it has) plus the
#     other uploads found.
#  4. after tagging, .m3u8 files are written when Preferences say so.
import json

import core

FINDCOPY_FILE = core.PIPELINE / "findcopy-run.json"
PLAYLISTS_FILE = core.PIPELINE / "playlists-run.json"


def check(run, job: dict) -> dict:
    """Step 1 and 2. Returns {"targets": [...], "plists": [...], "new": n}."""
    have, dead = core.archived_ids(), core.unavailable_ids()
    songs, plists, to_get, new_ids = {}, [], [], []
    run._step("find", "active", 0, len(job["urls"]))
    for n, url in enumerate(job["urls"], 1):
        run._check()
        name = job["names"].get(url) or url
        run._set(now=f"Checking {name} ({n} of {len(job['urls'])})")
        try:
            data = core.lookup(url)
        except Exception as e:   # noqa: BLE001 - one unreadable playlist mustn't stop the others
            run._problem(f"couldn't read {name}: {e}")
            run._step("find", done=n)
            continue
        entries = [e for e in (data.get("entries") or [data]) if e and e.get("id")]
        songs.update({e["id"]: {"title": e.get("title"), "artist": e.get("channel") or e.get("uploader"),
                                "duration": e.get("duration")} for e in entries})
        plists.append({"title": data.get("title") or "Playlist", "ids": [e["id"] for e in entries]})
        fresh = [e for e in entries if e["id"] not in have | dead and e["id"] not in new_ids]
        run._log(f"  {name}: {len(fresh)} new of {len(entries)}{core.dead_note(entries, dead)}")
        for e in fresh:
            new_ids.append(e["id"])
            run._add_song(title=e.get("title") or e["id"], artist=e.get("channel") or e.get("uploader") or "",
                          video=e["id"], state="queued", note=name)
        if fresh:
            to_get.append(url)
        run._step("find", done=n)
    run._step("find", "done")
    swapped = _swap(run, plists, songs)
    return {"targets": to_get + [f"https://music.youtube.com/watch?v={v}" for v in swapped],
            "plists": plists, "new": len(new_ids) + len(swapped)}


def _swap(run, plists: list[dict], songs: dict) -> list[str]:
    """Other uploads to download for songs YouTube won't serve (the old window's _swap_work + _swap_step)."""
    ids = list(dict.fromkeys(i for pl in plists for i in pl["ids"]))
    dead, have, rep = core.unavailable_ids(), core.archived_ids(), core.replacements()
    age = core.age_restricted_ids()   # findcopy may take someone else's exact upload of these
    look = [{"id": i, **songs[i], "age": i in age} for i in ids
            if i in dead and i not in rep and songs.get(i, {}).get("title")]
    get = [rep[i] for i in ids if rep.get(i) and rep[i] not in have and rep[i] not in dead]
    for v in get:
        run._add_song(title=v, artist="", video=v, state="found", note="another copy of an unavailable song")
    if not look:
        return get
    names = {x["id"]: x["title"] for x in look}
    run._log(f"Looking for another copy of {core.plural(len(look), 'unavailable song')}")
    FINDCOPY_FILE.write_text(json.dumps(look), encoding="utf-8")
    done = [0]

    def on_line(line: str):
        parts = line.split(" ", 3)
        if parts[0] not in ("found", "none", "error") or len(parts) < 2 or parts[1] not in names:
            return core.only_problems(line)
        done[0] += 1
        run._set(now=f"Looking for another copy {done[0]} of {len(look)}")
        old = parts[1]
        if parts[0] == "error":   # not written to replaced.txt, so it's searched again next run
            why = line.split(" ", 2)[2] if len(parts) > 2 else "unknown error"
            return f"  Couldn't search for {names[old]} ({why}), will try again next time"
        with core.REPLACED.open("a", encoding="utf-8") as f:
            if parts[0] == "found" and len(parts) == 4:
                f.write(f"{old} {parts[2]}  # {parts[3]}\n")
                get.append(parts[2])
                run._add_song(title=parts[3], artist="", video=parts[2], state="found",
                              note=f"another copy of {names[old]}")
                return f"  Found another copy: {parts[3]}"
            f.write(f"{old} -  # no other copy found: {names[old]}\n")
        return f"  No other copy found: {names[old]}"

    run._stream([core.PY, str(core.PIPELINE / "findcopy.py"), str(FINDCOPY_FILE)], on_line)
    return get


def pasted_playlists(run, urls: list[str]) -> list[dict]:
    """[{"title", "ids"}] for the YouTube playlists among pasted links, so a pasted playlist
    gets its .m3u8 like a saved one. A playlist that can't be read is reported and skipped:
    its songs still download."""
    plists = []
    for url in urls:
        if "list=" not in url:
            continue
        try:
            data = core.lookup(url)
        except Exception as e:   # noqa: BLE001 - only the playlist file is lost, not the download
            run._log(f"  couldn't read the playlist's track list, so no playlist file for it: {e}")
            continue
        ids = [e["id"] for e in data.get("entries") or [] if e and e.get("id")]
        if ids:
            plists.append({"title": data.get("title") or "Playlist", "ids": ids})
    return plists


def write_files(run, s: dict, plists: list[dict]) -> None:
    """Step 4: an .m3u8 per playlist, with other uploads in place of unavailable songs."""
    rep = core.replacements()
    PLAYLISTS_FILE.write_text(json.dumps([{**pl, "ids": [rep.get(i) or i for i in pl["ids"]]} for pl in plists]),
                              encoding="utf-8")
    run._step("lists", "active")
    run._set(now="Writing playlist files")
    run._stream([core.PY, str(core.PIPELINE / "writeplaylists.py"), str(core.write_run_config(s)), str(PLAYLISTS_FILE)],
                lambda ln: f"  {ln.strip()}" if ln.strip() else None)
    run._step("lists", "done")
