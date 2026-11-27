# Downloads for the new app window (app.pyw), run in the background while the window
# polls status(). Two kinds:
#  - "links": YouTube links (a song or playlist), like the classic window's Download.
#  - "album": songs picked on an album page of the search. findtracks.py finds each
#    on YouTube Music first; "found" ones are downloaded, "unsure" ones (MusicBrainz
#    has no length, so only title and artist matched) wait for the user to say
#    "download anyway", which sends the video id back. presettags.py then writes the
#    picked album and recording into the files before beets sees them.
# After downloading, the same steps as the classic window run, in the same order:
# squareart, cleantitles, (presettags), prunelib, beets import, fixartists, fillalbums,
# fillgenres, then beets write when ReplayGain is on. The classic window's helpers
# (settings, run-config, download archive, unavailable songs, newdownloads cleanup)
# are imported from gui.pyw so both windows behave the same.
# KEEP IN STEP with gui.pyw's _download_worker and get-music.ps1 when steps change.
import collections
import json
import re
import subprocess
import threading
from pathlib import Path

import gui
import musicinfo

PIPELINE = gui.PIPELINE
RAW = gui.RAW
FIND_FILE = PIPELINE / "findtracks-run.json"
PRESET_FILE = PIPELINE / "search-run.json"
MAX_URLS = 20
MAX_TRACKS = 200
REFUSED_LIMIT = 3        # YouTube refusing this many songs in a row: stop downloading, tag what came
LOG_KEEP = 300
VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
ID_IN_NAME = re.compile(r"\[([0-9A-Za-z_-]{11})\]")
# How much of the bar each step fills
STEPS = [("find", "Find", 0.12), ("download", "Download", 0.5), ("clean", "Clean up", 0.06),
         ("tag", "Tag", 0.14), ("albums", "Albums", 0.08), ("genres", "Genres", 0.1)]

# yt-dlp's output (same patterns as gui.pyw's _yt_filter)
ITEM_RE = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
URL_RE = re.compile(r"Extracting URL: .*(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})")
DEST_RE = re.compile(r"\[download\] Destination: (.+)")
PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")
SLEEP_RE = re.compile(r"Sleeping ([\d.]+) seconds")
ARCHIVED_RE = re.compile(r"\[download\] (.+?):? has already been recorded in the archive")
ERR_RE = re.compile(r"^ERROR: (?:\[\w+\] ([0-9A-Za-z_-]{11}): )?(.*)")


class Stopped(Exception):
    """The user pressed Stop."""


def _picks(raw, n_tracks: int) -> dict[int, str | None]:
    """{track index: forced video id or None} from the page's picks: ints, or {"i", "video"}
    for an "unsure" match the user said to download anyway."""
    if not isinstance(raw, list) or not raw or len(raw) > MAX_TRACKS:
        raise ValueError("pick at least one song")
    out = {}
    for p in raw:
        i, video = (p, None) if isinstance(p, int) else ((p or {}).get("i"), (p or {}).get("video"))
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < n_tracks:
            raise ValueError("a picked song isn't on that album")
        if video is not None and not (isinstance(video, str) and VIDEO_ID.match(video)):
            raise ValueError("bad YouTube video id")
        if video and video in out.values():
            raise ValueError("two songs can't be the same YouTube video")
        out[i] = video
    return out


def parse_job(job) -> dict:
    """A checked job from the page. Album track lists are read again from MusicBrainz (cached),
    not taken from the page."""
    if not isinstance(job, dict):
        raise ValueError("bad download request")
    if job.get("kind") == "links":
        urls = job.get("urls")
        if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_URLS:
            raise ValueError("paste one YouTube link")
        urls = [u.strip() for u in urls if isinstance(u, str)]
        if len(urls) != len(job["urls"]) or not all(gui.is_youtube_link(u) for u in urls):
            raise ValueError("that isn't a YouTube link")
        return {"kind": "links", "urls": urls, "label": "YouTube playlist" if any("list=" in u for u in urls) else "YouTube link"}
    if job.get("kind") == "album":
        data = musicinfo.release(job.get("release_id"))
        picks = _picks(job.get("picks"), len(data["tracks"]))
        rel = data["release"]
        return {"kind": "album", "release": rel, "tracks": data["tracks"], "picks": picks,
                "label": f"{rel['title']} · {rel['artist']}"}
    raise ValueError("unknown kind of download")


class DownloadRun:
    """One download at a time, in the background."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self._cut = False      # a filter decided to end the current step early
        self._state: dict = {"running": False, "seq": 0}

    # ---------- for the window ----------

    def running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def start(self, job: dict) -> dict:
        """job: a checked job from parse_job() (which may ask MusicBrainz, so callers do it before locking)."""
        with self._lock:
            if self._state["running"]:
                raise ValueError("a download is already running")
            album = job["kind"] == "album"
            songs = [{"i": i, "title": job["tracks"][i]["title"], "artist": job["tracks"][i]["artist"],
                      "video": v, "state": "found" if v else "queued", "note": ""} for i, v in sorted(job["picks"].items())] if album else []
            self._stopping, self._cut = False, False
            self._state = {
                "running": True, "seq": self._state["seq"] + 1, "label": job["label"], "kind": job["kind"],
                "release_id": job["release"]["id"] if album else None, "result": None, "summary": "", "problems": [],
                "now": "Starting", "steps": [{"key": k, "name": n, "state": "skipped" if k == "find" and not album else "waiting",
                                              "done": 0, "total": 0} for k, n, _ in STEPS],
                "songs": songs, "log": [],
            }
        threading.Thread(target=self._worker, args=(job,), daemon=True).start()
        return self.status()

    def stop(self) -> bool:
        with self._lock:
            if not self._state["running"]:
                return False
            self._stopping = True
            proc = self._proc
            self._state["now"] = "Stopping…"
        if proc:
            gui.kill_tree(proc.pid)
        return True

    def status(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self._state))
        if "steps" in st:
            st["progress"] = _progress(st["steps"])
        return st

    # ---------- state, under the lock ----------

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    def _log(self, line: str) -> None:
        with self._lock:
            self._state["log"] = (self._state["log"] + [line])[-LOG_KEEP:]

    def _problem(self, text: str) -> None:
        with self._lock:
            self._state["problems"].append(text)
            self._state["log"] = (self._state["log"] + ["  Problem: " + text])[-LOG_KEEP:]

    def _step(self, key: str, state: str | None = None, done: int | None = None, total: int | None = None) -> None:
        with self._lock:
            for s in self._state["steps"]:
                if s["key"] == key:
                    if state:
                        s["state"] = state
                    if done is not None:
                        s["done"] = done
                    if total is not None:
                        s["total"] = total

    def _song(self, match, **kw) -> dict | None:
        with self._lock:
            for s in self._state["songs"]:
                if match(s):
                    s.update(kw)
                    return s
        return None

    def _add_song(self, **kw) -> None:
        with self._lock:
            self._state["songs"].append({"i": None, "video": None, "note": "", **kw})

    # ---------- running steps ----------

    def _check(self) -> None:
        if self._stopping:
            raise Stopped

    def _stream(self, cmd: list[str], on_line=None, ytdlp: bool = False) -> int:
        """Run one step; every line goes to last-run.log. on_line(line) handles it for the window."""
        self._check()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", env=gui.child_env(), creationflags=gui.NO_WINDOW, bufsize=1, cwd=PIPELINE)
        with self._lock:
            self._proc, self._cut = p, False
            stop_now = self._stopping   # Stop pressed while this was starting: it found no process to end
        tail = collections.deque(maxlen=5)
        try:
            if stop_now:
                gui.kill_tree(p.pid)
            for line in p.stdout:
                gui.runlog(line)
                if line.strip():
                    tail.append(line.strip())
                shown = (on_line or gui.App._only_problems)(line.rstrip())
                if shown:
                    self._log(shown)
                if self._cut:
                    gui.kill_tree(p.pid)
                    break
        except BaseException:
            # Never leave a step running unwatched: the job would look finished while
            # beets or yt-dlp still writes to the library and newdownloads.
            gui.kill_tree(p.pid)
            raise
        finally:
            p.wait()
            with self._lock:
                self._proc = None
        self._check()
        # yt-dlp exits with 1 whenever one song failed, which is reported per song
        if p.returncode and not ytdlp and not self._cut:
            name = f"beets {cmd[5]}" if cmd[1] == "-m" else Path(cmd[1]).name   # [PY, -m, beets, -c, cfg, import]
            errs = [ln for ln in tail if gui.App._only_problems(ln) and "traceback" not in ln.lower()]
            self._problem(f"{name} crashed: {(errs or list(tail) or [f'exit code {p.returncode}'])[-1]}")
        return p.returncode

    def _counted(self, key: str, prefixes: tuple[str, ...]):
        """A line handler counting songs done in a step (lines starting with prefixes)."""
        def on_line(line: str):
            if line.strip().startswith(prefixes):
                with self._lock:
                    step = next(s for s in self._state["steps"] if s["key"] == key)
                    step["done"] = min(step["done"] + 1, step["total"] or step["done"] + 1)
            return gui.App._only_problems(line)
        return on_line

    def _find(self, job: dict) -> list[tuple[int, str]]:
        """findtracks.py on the picked tracks that have no video yet. Returns [(track index, video id)] to download."""
        want = [i for i, v in sorted(job["picks"].items()) if not v]
        get = [(i, v) for i, v in sorted(job["picks"].items()) if v]
        if not want:
            self._step("find", "skipped")
            return get
        self._step("find", "active", 0, len(want))
        self._set(now=f"Finding {gui.plural(len(want), 'song')} on YouTube Music")
        for i in want:
            self._song(lambda s, i=i: s["i"] == i, state="finding")
        FIND_FILE.write_text(json.dumps({"release": job["release"], "tracks": [job["tracks"][i] for i in want]},
                                        ensure_ascii=False), encoding="utf-8")
        pending = list(want)   # findtracks answers in track order
        found: list[tuple[int, str]] = []

        def on_line(line: str):
            parts = line.split(" ", 3)
            if parts[0] == "playlist" and len(parts) > 2:
                return f"  Found the album on YouTube Music ({parts[2]} tracks from its playlist)"
            if parts[0] not in ("found", "unsure", "none", "error") or len(parts) < 2 or not pending:
                return gui.App._only_problems(line)
            i = pending.pop(0)
            title = job["tracks"][i]["title"]
            with self._lock:
                step = next(s for s in self._state["steps"] if s["key"] == "find")
                step["done"] += 1
            if parts[0] == "found" and len(parts) > 2:
                found.append((i, parts[2]))
                self._song(lambda s: s["i"] == i, state="found", video=parts[2], note=parts[3] if len(parts) > 3 else "")
                return None
            if parts[0] == "unsure" and len(parts) > 2:
                self._song(lambda s: s["i"] == i, state="unsure", video=parts[2], note=parts[3] if len(parts) > 3 else "")
                return f"  Check this match: {title} -> {parts[3] if len(parts) > 3 else parts[2]}"
            if parts[0] == "none":
                self._song(lambda s: s["i"] == i, state="notfound", note="No match on YouTube Music")
                return f"  Not found on YouTube Music: {title}"
            why = line.split(" ", 2)[2] if len(line.split(" ", 2)) > 2 else "unknown error"
            self._song(lambda s: s["i"] == i, state="searchfail", note=why)
            return f"  Couldn't search for {title} ({why})"

        self._stream([gui.PY, str(PIPELINE / "findtracks.py"), str(FIND_FILE)], on_line)
        for i in pending:   # findtracks stopped early (crashed): those weren't searched
            self._song(lambda s, i=i: s["i"] == i, state="searchfail", note="the search stopped early")
        self._step("find", "done")
        return sorted(get + found)

    def _one_track_per_video(self, get: list[tuple[int, str]], tracks: list[dict]) -> list[tuple[int, str]]:
        """findtracks can match one video to two tracks. A file can't be both, so only the first track
        downloads; the other is marked (presettags would otherwise skip both)."""
        seen, out = {}, []
        for i, v in get:
            if v in seen:
                self._song(lambda x, i=i: x["i"] == i, state="searchfail",
                           note=f"same YouTube video as “{tracks[seen[v]]['title']}”")
                self._log(f"  Same video found for two tracks, kept for {tracks[seen[v]]['title']} only: {tracks[i]['title']}")
                continue
            seen[v] = i
            out.append((i, v))
        return out

    def _download(self, targets: list[str], s: dict) -> dict:
        """yt-dlp on the targets, with the classic window's options. Returns {"songs", "blocked", "dead"}."""
        if s["format"] == "mp3":
            fmt_args = ["-f", "bestaudio", "-x", "--audio-format", "mp3", "--audio-quality", "2"]
        else:
            fmt_args = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a", "--audio-quality", "160K"]
        pause = int(s.get("pause") or 0)
        pause_args = ["--sleep-requests", "0.75", "--sleep-interval", str(pause), "--max-sleep-interval", str(pause * 2)] if pause else []
        st = {"n": 0, "total": len(targets), "songs": 0, "refused": 0, "blocked": False, "id": None, "title": ""}
        ytlog: list[str] = []
        self._step("download", "active", 0, len(targets))
        self._set(now=f"Starting the download of {gui.plural(len(targets), 'song')}")

        def finish_current():
            if st["id"]:
                self._song(lambda x: x["video"] == st["id"] and x["state"] == "downloading", state="downloaded")

        def on_line(line: str):
            ytlog.append(line)
            if m := ITEM_RE.search(line):
                st["n"], st["total"] = int(m.group(1)), int(m.group(2))
                self._step("download", total=st["total"])
                return None
            if m := URL_RE.search(line):
                finish_current()
                st["id"] = m.group(1)
                return None
            if m := ARCHIVED_RE.search(line):
                # yt-dlp checks the archive before looking the video up, so the id is on this line
                vid = m.group(1) if VIDEO_ID.match(m.group(1)) else st["id"]
                note = "Downloaded before (in downloaded.txt)"
                if not self._song(lambda x: x["video"] == vid, state="archived", note=note):
                    self._add_song(title=m.group(1) if vid != m.group(1) else f"YouTube video {vid}", artist="",
                                   video=vid, state="archived", note=note)
                st["n"] = st["n"] or 1
                self._step("download", done=st["n"])
                return None
            if m := DEST_RE.search(line):
                path = Path(m.group(1))
                vid = (ID_IN_NAME.search(path.name) or [None, st["id"]])[1]
                st["id"], st["title"], st["refused"] = vid, gui.strip_id(path.stem), 0
                if path.suffix.lower() in (".jpg", ".webp", ".png"):
                    return None
                st["songs"] += 1
                if not self._song(lambda x: x["video"] == vid, state="downloading"):
                    self._add_song(title=st["title"], artist="", video=vid, state="downloading")
                self._step("download", done=max(st["n"], st["songs"]) - 1)
                self._set(now=f"Downloading {st['n'] or st['songs']} of {st['total']}: {st['title']}")
                return f"  {st['title']}"
            if m := SLEEP_RE.search(line):
                self._set(now=f"Waiting {round(float(m.group(1)))} s before the next song (so YouTube doesn't block you)")
                return None
            if m := PCT_RE.search(line):
                if st["title"]:
                    self._set(now=f"Downloading {st['n'] or st['songs']} of {st['total']}: {st['title']} {m.group(1)}%")
                return None
            if m := ERR_RE.search(line):
                vid, reason = m.group(1) or st["id"], m.group(2)
                if "403" in reason:
                    st["refused"] += 1
                    if st["refused"] >= REFUSED_LIMIT:
                        st["blocked"], self._cut = True, True
                    why = "YouTube refused the download"
                elif "unavailable" in reason.lower() or "not available" in reason.lower():
                    why = "not available on YouTube"
                else:
                    why = reason
                # A line naming no video is about the song in progress: never flip one that already finished
                unfinished = ("queued", "found", "downloading")
                if not self._song(lambda x: x["video"] == vid and (m.group(1) or x["state"] in unfinished), state="failed", note=why) \
                        and m.group(1):
                    self._add_song(title=vid, artist="", video=vid, state="failed", note=why)
                return f"  Failed: {why}"
            return None

        try:
            self._stream([gui.PY, str(gui.YTDLP), "--ffmpeg-location", str(gui.BIN), "--remote-components", "ejs:github",
                          *fmt_args, *pause_args, "--embed-thumbnail", "--embed-metadata", "--convert-thumbnails", "jpg",
                          "--parse-metadata", ":(?P<meta_genre>)",   # YouTube's category ("Music") would be the genre
                          "--no-overwrites", "--ignore-errors", "--newline", "--download-archive", str(gui.ARCHIVE),
                          "-o", str(RAW / "%(title)s [%(id)s].%(ext)s"),
                          "--", *targets], on_line, ytdlp=True)   # "--": a link can never be read as an option
        except Stopped:
            gui.record_unavailable(ytlog)   # like the classic window: songs YouTube won't serve aren't retried
            raise
        finish_current()
        self._step("download", "done", st["total"])
        return {"songs": st["songs"], "blocked": st["blocked"], "dead": gui.record_unavailable(ytlog)}

    def _tag(self, s: dict, run_cfg: str, preset: dict | None) -> dict:
        """Everything after downloading, in the classic window's order."""
        n_files = len(gui.raw_songs())
        self._step("clean", "active", 0, n_files)
        self._set(now="Fixing cover art and titles")
        if s["square_art"]:
            self._stream([gui.PY, str(PIPELINE / "squareart.py"), str(RAW), "300" if s["small_art"] else "500"],
                         self._counted("clean", ("art:",)))
        self._stream([gui.PY, str(PIPELINE / "cleantitles.py"), str(RAW)])
        if preset:
            PRESET_FILE.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")
            self._stream([gui.PY, str(PIPELINE / "presettags.py"), str(PRESET_FILE), str(RAW)])
        self._step("clean", "done", n_files)

        self._step("tag", "active", 0, n_files)
        self._set(now=f"Matching {gui.plural(n_files, 'song')} on MusicBrainz")
        self._stream([gui.PY, str(PIPELINE / "prunelib.py"), run_cfg])   # forget songs deleted from the folder
        beets_log: list[str] = []
        counting = self._counted("tag", (str(RAW),))

        def on_beets(line: str):
            beets_log.append(line)
            return counting(line)

        beets_ok = self._stream([gui.PY, "-m", "beets", "-c", run_cfg, "import", "-q", "-s", str(RAW)], on_beets) == 0
        n_asis = sum(1 for ln in beets_log if "Importing as-is" in ln)
        dupes, current = [], None   # already in the library: beets leaves the extra copy in newdownloads
        for ln in beets_log:
            if ln.strip().startswith(str(RAW)):
                current = Path(ln.strip())
            elif "already in the library" in ln and current:
                dupes.append(current)
        for p in dupes:
            self._log(f"  Skipped, already in your library: {gui.strip_id(p.stem)}")
            vid = (ID_IN_NAME.search(p.name) or [None, None])[1]
            self._song(lambda x: x["video"] == vid and vid, state="have", note="Already in your library")
            try:
                p.unlink()
            except OSError:
                pass
        self._step("tag", "done", n_files)

        n_matched = max(n_files - n_asis - len(dupes), 0) if beets_ok else 0
        self._step("albums", "active", 0, n_matched)
        self._set(now="Artists, albums and years")
        self._stream([gui.PY, str(PIPELINE / "fixartists.py"), run_cfg])
        album_log: list[str] = []
        counting_albums = self._counted("albums", ("album:",))

        def on_albums(line: str):
            album_log.append(line)
            return counting_albums(line)

        self._stream([gui.PY, str(PIPELINE / "fillalbums.py"), run_cfg], on_albums)
        self._step("albums", "done")

        n_new = max(n_files - len(dupes), 0)
        self._step("genres", "active", 0, n_new)
        self._set(now="Genres from Last.fm")
        self._stream([gui.PY, str(PIPELINE / "fillgenres.py"), run_cfg], self._counted("genres", ("genre:",)))
        if s["replaygain"]:
            # beets doesn't always write the loudness values into files it couldn't match
            self._stream([gui.PY, "-m", "beets", "-c", run_cfg, "write"])
        self._step("genres", "done")
        return {"files": n_files, "dupes": len(dupes), "asis": n_asis, "matched": n_matched,
                "albums": sum(1 for ln in album_log if 'album "' in ln)}

    def _worker(self, job: dict) -> None:
        result = "failed"
        try:
            s = gui.load_settings()
            run_cfg = str(gui.write_run_config(s))
            gui.App.start_runlog(f"New window: {job['label']}")
            preset, targets = None, list(job.get("urls") or [])
            if job["kind"] == "album":
                get = self._one_track_per_video(self._find(job), job["tracks"])
                targets = [f"https://music.youtube.com/watch?v={v}" for _, v in get]
                for i, v in get:
                    self._song(lambda x, i=i: x["i"] == i, video=v)
                t = job["tracks"]
                # no recording id (rare): it still downloads, beets tags it the usual way
                preset = {"release": job["release"], "tracks": [{
                    "video_id": v, "recording_id": t[i]["recording_id"], "title": t[i]["title"], "artist": t[i]["artist"],
                    "disc": t[i].get("disc"), "n": t[i].get("n")} for i, v in get if t[i].get("recording_id")]}
            yt = {"songs": 0, "blocked": False, "dead": 0}
            if targets:
                yt = self._download(targets, s)
                if yt["blocked"]:
                    self._log("  YouTube is refusing downloads right now, so downloading stopped. "
                              "The songs that did download still get tagged.")
            else:
                self._step("download", "skipped")
            gui.clean_raw()
            self._check()
            # Always, like the classic window, even with nothing new: prunelib forgets deleted songs
            # and fillgenres backfills Last.fm tags for older songs on every run.
            tagged = self._tag(s, run_cfg, preset)
            gui.clean_raw()
            with self._lock:
                for x in self._state["songs"]:
                    if x["state"] == "downloaded":
                        x["state"] = "done"
            result = "done"
            self._set(summary=_summary(yt, tagged, self.status()["songs"]))
        except Stopped:
            result = "stopped"
            gui.clean_raw()   # half-finished files; finished ones stay and are tagged next time
            self._set(summary="Stopped. Anything already downloaded is tagged on the next download.")
        except Exception as e:   # noqa: BLE001 - shown in the window and the log; the run must end cleanly
            gui.runlog(f"!! new window download crashed: {type(e).__name__}: {e}\n")
            self._problem(f"{type(e).__name__}: {e}")
            self._set(summary="The download stopped with a problem.")
        finally:
            with self._lock:
                self._state.update(running=False, result=result, now="")
                for x in self._state["steps"]:
                    if x["state"] == "active":
                        x["state"] = "stopped"
                    elif x["state"] == "waiting":   # never got to it
                        x["state"] = "notrun"


def _progress(steps: list[dict]) -> float:
    weight = {k: w for k, _, w in STEPS}
    live = [s for s in steps if s["state"] != "skipped"]
    total = sum(weight[s["key"]] for s in live) or 1
    done = 0.0
    for s in live:
        if s["state"] == "done":
            done += weight[s["key"]]
        elif s["state"] == "active" and s["total"]:
            done += weight[s["key"]] * min(s["done"] / s["total"], 1)
    return round(done / total, 3)


def _summary(yt: dict, tagged: dict | None, songs: list[dict]) -> str:
    parts = [f"{gui.plural(yt['songs'], 'song')} downloaded"]
    count = collections.Counter(x["state"] for x in songs)
    if yt["blocked"]:
        parts.append("stopped early because YouTube is blocking downloads")
    if count["unsure"]:
        parts.append(f"{count['unsure']} to check")
    if count["notfound"]:
        parts.append(f"{count['notfound']} not found on YouTube Music")
    if count["archived"]:
        parts.append(f"{count['archived']} downloaded before")
    if yt["dead"]:
        parts.append(f"{yt['dead']} unavailable")
    if tagged:
        if tagged["dupes"]:
            parts.append(f"{tagged['dupes']} already in library")
        if tagged["matched"]:
            parts.append(f"{tagged['matched']} matched on MusicBrainz")
        if tagged["asis"]:
            parts.append(f"{tagged['asis']} kept YouTube's info")
    return ", ".join(parts)
