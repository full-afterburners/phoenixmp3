# Downloads for the new app window (app.pyw), run in the background while the window
# polls status(). A download started while one runs waits in a queue. Kinds:
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
# come from core.py.
# KEEP IN STEP with get-music.ps1 (the command line version) when steps change.
import collections
import datetime
import json
import re
import subprocess
import threading
from pathlib import Path

import core
import musicinfo
import myplaylists
import playlistrun

PIPELINE = core.PIPELINE
RAW = core.RAW
FIND_FILE = PIPELINE / "findtracks-run.json"
PRESET_FILE = PIPELINE / "search-run.json"
MAX_URLS = 20
MAX_TRACKS = 200
REFUSED_LIMIT = 3        # YouTube refusing this many songs in a row: stop downloading, tag what came
LOG_KEEP = 300
MAX_QUEUE = 20           # downloads waiting behind the running one
FINISHED_KEEP = 10       # finished downloads listed in Activity
VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")
ID_IN_NAME = re.compile(r"\[([0-9A-Za-z_-]{11})\]")
AGE_NOTE = "YouTube wants a sign-in to confirm your age, and no other upload matched"
REUPLOAD_NOTE = "Someone else's upload: YouTube's own needs a sign-in to confirm your age"
# How much of the bar each step fills
STEPS = [("find", "Find", 0.12), ("download", "Download", 0.5), ("clean", "Clean up", 0.06),
         ("tag", "Tag", 0.14), ("albums", "Albums", 0.08), ("genres", "Genres", 0.1), ("lists", "Playlist files", 0.02)]

# yt-dlp's output
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
        if len(urls) != len(job["urls"]) or not all(core.is_youtube_link(u) for u in urls):
            raise ValueError("that isn't a YouTube link")
        return {"kind": "links", "urls": urls, "label": "YouTube playlist" if any("list=" in u for u in urls) else "YouTube link"}
    if job.get("kind") == "album":
        data = musicinfo.release(job.get("release_id"))
        picks = _picks(job.get("picks"), len(data["tracks"]))
        rel = data["release"]
        return {"kind": "album", "release": rel, "tracks": data["tracks"], "picks": picks,
                "label": f"{rel['title']} · {rel['artist']}"}
    if job.get("kind") == "playlists":
        saved = {p["url"]: p["name"] for p in myplaylists.read()}
        urls = job.get("urls")
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u in saved for u in urls):
            raise ValueError("those aren't in My Playlists")
        urls = list(dict.fromkeys(urls))
        return {"kind": "playlists", "urls": urls, "names": {u: saved[u] for u in urls},
                "label": saved[urls[0]] if len(urls) == 1 else f"My Playlists ({len(urls)})"}
    raise ValueError("unknown kind of download")


def _job_key(job: dict) -> str:
    """What makes two checked jobs the same download, to refuse queueing one twice."""
    return json.dumps({"kind": job["kind"], "urls": job.get("urls"), "picks": job.get("picks"),
                       "release": (job.get("release") or {}).get("id")}, sort_keys=True)


class DownloadRun:
    """One download at a time, in the background. Downloads started while one runs wait in a
    queue (in memory: closing the app forgets it) and start one after another; the run never
    shows as "not running" between two of them, so a re-file or a settings change can't slip in."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stopping = False
        self._cut = False      # a filter decided to end the current step early
        self._state: dict = {"running": False, "seq": 0}
        self._key = ""         # _job_key of the running download
        self._queue: list[dict] = []      # [{"qid", "key", "job"}], next first
        self._finished: list[dict] = []   # [{"seq", "label", "result", "summary"}], newest first
        self._qid = 0

    # ---------- for the window ----------

    def running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def start(self, job: dict) -> dict:
        """job: a checked job from parse_job() (which may ask MusicBrainz, so callers do it before locking).
        Starts it, or queues it behind the running one; the answer's "queued" says which."""
        key = _job_key(job)
        with self._lock:
            queued = self._state["running"]
            if queued:
                if key == self._key:
                    raise ValueError("that's downloading right now")
                if any(q["key"] == key for q in self._queue):
                    raise ValueError("that's already waiting in the queue")
                if len(self._queue) >= MAX_QUEUE:
                    raise ValueError(f"the queue is full ({MAX_QUEUE} downloads); add more when some have finished")
                self._qid += 1
                self._queue.append({"qid": self._qid, "key": key, "job": job})
            else:
                self._begin(job, key)
        if not queued:
            threading.Thread(target=self._worker, args=(job, False), daemon=True).start()
        return {**self.status(), "queued": queued}

    def stop(self, everything: bool = False) -> bool:
        """Stop the running download; the next queued one then starts, unless everything=True."""
        with self._lock:
            if not self._state["running"]:
                return False
            if everything:
                self._queue = []
            self._stopping = True
            proc = self._proc
            self._state["now"] = "Stopping…"
        if proc:
            core.kill_tree(proc.pid)
        return True

    def unqueue(self, qid) -> bool:
        """Take a waiting download out of the queue. False if it already started (or never was)."""
        with self._lock:
            before = len(self._queue)
            self._queue = [q for q in self._queue if q["qid"] != qid]
            return len(self._queue) < before

    def status(self) -> dict:
        with self._lock:
            st = json.loads(json.dumps(self._state))
            st["queue"] = [{"qid": q["qid"], "label": q["job"]["label"], "kind": q["job"]["kind"],
                            "urls": q["job"]["urls"] if q["job"]["kind"] == "links" else []} for q in self._queue]
            st["finished"] = [dict(f) for f in self._finished]
        if "steps" in st:
            st["progress"] = _progress(st["steps"])
        return st

    def _begin(self, job: dict, key: str) -> None:
        """Make job the running download. Under the lock; the caller starts its worker thread."""
        album = job["kind"] == "album"
        songs = [{"i": i, "title": job["tracks"][i]["title"], "artist": job["tracks"][i]["artist"],
                  "video": v, "state": "found" if v else "queued", "note": ""} for i, v in sorted(job["picks"].items())] if album else []
        steps = [_first_step(k, n, job, core.load_settings()) for k, n, _ in STEPS]   # may raise: before changing anything
        self._stopping, self._cut, self._key = False, False, key
        self._state = {
            "running": True, "seq": self._state["seq"] + 1, "label": job["label"], "kind": job["kind"],
            "release_id": job["release"]["id"] if album else None, "result": None, "summary": "", "problems": [],
            "now": "Starting", "steps": steps, "songs": songs, "log": [],
        }

    def _end(self, result: str) -> None:
        """The running download is over: record it, then start the next queued one, if any."""
        nxt = None
        with self._lock:
            st = self._state
            for x in st["steps"]:
                if x["state"] == "active":
                    x["state"] = "stopped"
                elif x["state"] == "waiting":   # never got to it
                    x["state"] = "notrun"
            self._finished = ([{"seq": st["seq"], "label": st["label"], "result": result, "summary": st["summary"]}]
                              + self._finished)[:FINISHED_KEEP]
            while self._queue and nxt is None:
                q = self._queue.pop(0)
                try:
                    self._begin(q["job"], q["key"])
                    nxt = q["job"]
                except Exception as e:   # noqa: BLE001 - e.g. settings.json unreadable: skip that one, keep the queue going
                    core.runlog(f"!! couldn't start the queued download {q['job']['label']}: {type(e).__name__}: {e}\n")
            if nxt is None:
                st.update(running=False, result=result, now="")
                self._key = ""
        if nxt is not None:
            threading.Thread(target=self._worker, args=(nxt, True), daemon=True).start()

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
                             errors="replace", env=core.child_env(), creationflags=core.NO_WINDOW, bufsize=1, cwd=PIPELINE)
        with self._lock:
            self._proc, self._cut = p, False
            stop_now = self._stopping   # Stop pressed while this was starting: it found no process to end
        tail = collections.deque(maxlen=5)
        try:
            if stop_now:
                core.kill_tree(p.pid)
            for line in p.stdout:
                core.runlog(line)
                if line.strip():
                    tail.append(line.strip())
                shown = (on_line or core.only_problems)(line.rstrip())
                if shown:
                    self._log(shown)
                if self._cut:
                    core.kill_tree(p.pid)
                    break
        except BaseException:
            # Never leave a step running unwatched: the job would look finished while
            # beets or yt-dlp still writes to the library and newdownloads.
            core.kill_tree(p.pid)
            raise
        finally:
            p.wait()
            with self._lock:
                self._proc = None
        self._check()
        # yt-dlp exits with 1 whenever one song failed, which is reported per song
        if p.returncode and not ytdlp and not self._cut:
            name = f"beets {cmd[5]}" if cmd[1] == "-m" else Path(cmd[1]).name   # [PY, -m, beets, -c, cfg, import]
            errs = [ln for ln in tail if core.only_problems(ln) and "traceback" not in ln.lower()]
            self._problem(f"{name} crashed: {(errs or list(tail) or [f'exit code {p.returncode}'])[-1]}")
        return p.returncode

    def _counted(self, key: str, prefixes: tuple[str, ...]):
        """A line handler counting songs done in a step (lines starting with prefixes)."""
        def on_line(line: str):
            if line.strip().startswith(prefixes):
                with self._lock:
                    step = next(s for s in self._state["steps"] if s["key"] == key)
                    step["done"] = min(step["done"] + 1, step["total"] or step["done"] + 1)
            return core.only_problems(line)
        return on_line

    def _find(self, job: dict, gated: dict[int, str] | None = None) -> list[tuple[int, str]]:
        """findtracks.py on the picked tracks that have no video yet. Returns [(track index, video id)] to download.
        gated: {track index: video id} of tracks whose video turned out age-restricted when downloading;
        only someone else's exact upload is searched for those (findtracks --reupload)."""
        if gated:
            want, get = sorted(gated), []
        else:
            want = [i for i, v in sorted(job["picks"].items()) if not v]
            get = [(i, v) for i, v in sorted(job["picks"].items()) if v]
        if not want:
            self._step("find", "skipped")
            return get
        self._step("find", "active", 0, len(want))
        self._set(now=f"Looking for other uploads of {core.plural(len(want), 'age-restricted song')}" if gated
                  else f"Finding {core.plural(len(want), 'song')} on YouTube Music")
        for i in want:
            self._song(lambda s, i=i: s["i"] == i, state="finding")
        tracks = [{**job["tracks"][i], **({"skip": gated[i]} if gated else {})} for i in want]
        FIND_FILE.write_text(json.dumps({"release": job["release"], "tracks": tracks}, ensure_ascii=False), encoding="utf-8")
        pending = list(want)   # findtracks answers in track order
        found: list[tuple[int, str]] = []

        def on_line(line: str):
            parts = line.split(" ", 3)
            if parts[0] == "playlist" and len(parts) > 2:
                return f"  Found the album on YouTube Music ({parts[2]} tracks from its playlist)"
            if parts[0] not in ("found", "unsure", "none", "error", "reupload", "agegated") or len(parts) < 2 or not pending:
                return core.only_problems(line)
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
            if parts[0] == "reupload" and len(parts) > 2:
                found.append((i, parts[2]))
                self._song(lambda s: s["i"] == i, state="found", video=parts[2], note=REUPLOAD_NOTE)
                return f"  Age-restricted on YouTube, using someone else's upload: {title} -> {parts[3] if len(parts) > 3 else parts[2]}"
            if parts[0] == "agegated":
                self._song(lambda s: s["i"] == i, state="agegated", note=AGE_NOTE)
                return f"  Age-restricted on YouTube, no other upload matched: {title}"
            if parts[0] == "none":
                self._song(lambda s: s["i"] == i, state="notfound", note="No match on YouTube Music")
                return f"  Not found on YouTube Music: {title}"
            why = line.split(" ", 2)[2] if len(line.split(" ", 2)) > 2 else "unknown error"
            self._song(lambda s: s["i"] == i, state="searchfail", note=why)
            return f"  Couldn't search for {title} ({why})"

        self._stream([core.PY, str(PIPELINE / "findtracks.py"), str(FIND_FILE), *(["--reupload"] if gated else [])], on_line)
        for i in pending:   # findtracks stopped early (crashed): those weren't searched
            self._song(lambda s, i=i: s["i"] == i, state="searchfail", note="the search stopped early")
        self._step("find", "done")
        return sorted(get + found)

    def _retry_age_gated(self, job: dict, s: dict, get: list[tuple[int, str]], yt: dict) -> list[tuple[int, str]]:
        """Album tracks taken from YouTube Music's album playlist aren't checked before downloading, so an
        age-restricted one only shows up as a failed download. Look for someone else's exact upload of those
        and download what's found. Returns the album's [(track index, video id)] with the new videos in place."""
        with self._lock:
            gated = {x["i"]: x["video"] for x in self._state["songs"]
                     if x["state"] == "agegated" and x.get("i") is not None and x.get("video")}
        if not gated:
            return get
        used = {v for i, v in get if i not in gated}
        more = [(i, v) for i, v in self._find(job, gated) if v not in used]
        if not more:
            return get
        for i, v in more:
            self._song(lambda x, i=i: x["i"] == i, video=v)
        again = self._download([f"https://music.youtube.com/watch?v={v}" for _, v in more], s)
        yt["songs"] += again["songs"]
        yt["dead"] += again["dead"]
        yt["blocked"] = yt["blocked"] or again["blocked"]
        swap = dict(more)
        return [(i, swap.get(i, v)) for i, v in get]

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

    def _download(self, targets: list[str], s: dict, known_only: bool = False) -> dict:
        """yt-dlp on the targets, with the classic window's options. Returns {"songs", "blocked", "dead"}.
        known_only: a playlist lists every song it has had; only new ones (already in the list) get a row."""
        if s["format"] == "mp3":
            fmt_args = ["-f", "bestaudio", "-x", "--audio-format", "mp3", "--audio-quality", "2"]
        else:
            fmt_args = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x", "--audio-format", "m4a", "--audio-quality", "160K"]
        pause = int(s.get("pause") or 0)
        pause_args = ["--sleep-requests", "0.75", "--sleep-interval", str(pause), "--max-sleep-interval", str(pause * 2)] if pause else []
        st = {"n": 0, "total": len(targets), "songs": 0, "refused": 0, "blocked": False, "id": None, "title": ""}
        ytlog: list[str] = []
        self._step("download", "active", 0, len(targets))
        self._set(now=f"Starting the download of {core.plural(len(targets), 'song')}")

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
                # A single link: yt-dlp checks the archive before looking the video up, so the id is on the line.
                # In a playlist it's the song's title, and the last id seen belongs to another song.
                said = m.group(1)
                vid = said if VIDEO_ID.match(said) else None if known_only else st["id"]
                note = "Downloaded before (in downloaded.txt)"
                waiting = ("queued", "found")   # never relabel a song that's downloading or done
                hit = self._song(lambda x: x["state"] in waiting and (x["video"] == vid if vid else x["title"].lower() == said.lower()),
                                 state="archived", note=note)
                if not hit and not known_only:
                    self._add_song(title=m.group(1) if vid != m.group(1) else f"YouTube video {vid}", artist="",
                                   video=vid, state="archived", note=note)
                st["n"] = st["n"] or 1
                self._step("download", done=st["n"])
                return None
            if m := DEST_RE.search(line):
                path = Path(m.group(1))
                vid = (ID_IN_NAME.search(path.name) or [None, st["id"]])[1]
                st["id"], st["title"], st["refused"] = vid, core.strip_id(path.stem), 0
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
                elif any(w in reason.lower() for w in core.AGE_WORDS):
                    why = "age-restricted: YouTube wants a sign-in to confirm your age"
                elif "unavailable" in reason.lower() or "not available" in reason.lower():
                    why = "not available on YouTube"
                else:
                    why = reason
                state = "agegated" if why.startswith("age-restricted") else "failed"
                # A line naming no video is about the song in progress: never flip one that already finished
                unfinished = ("queued", "found", "downloading")
                if not self._song(lambda x: x["video"] == vid and (m.group(1) or x["state"] in unfinished), state=state, note=why) \
                        and m.group(1) and not known_only:
                    self._add_song(title=vid, artist="", video=vid, state=state, note=why)
                return f"  Failed: {why}"
            return None

        try:
            self._stream([core.PY, str(core.YTDLP), "--ffmpeg-location", str(core.BIN), "--remote-components", "ejs:github",
                          *fmt_args, *pause_args, "--embed-thumbnail", "--embed-metadata", "--convert-thumbnails", "jpg",
                          "--parse-metadata", ":(?P<meta_genre>)",   # YouTube's category ("Music") would be the genre
                          "--no-overwrites", "--ignore-errors", "--newline", "--download-archive", str(core.ARCHIVE),
                          "-o", str(RAW / "%(title)s [%(id)s].%(ext)s"),
                          "--", *targets], on_line, ytdlp=True)   # "--": a link can never be read as an option
        except Stopped:
            core.record_unavailable(ytlog)   # like the classic window: songs YouTube won't serve aren't retried
            raise
        finish_current()
        self._step("download", "done", st["total"])
        return {"songs": st["songs"], "blocked": st["blocked"], "dead": core.record_unavailable(ytlog)}

    def _tag(self, s: dict, run_cfg: str, preset: dict | None) -> dict:
        """Everything after downloading, in the classic window's order."""
        n_files = len(core.raw_songs())
        self._step("clean", "active", 0, n_files)
        self._set(now="Fixing cover art and titles")
        if s["square_art"]:
            self._stream([core.PY, str(PIPELINE / "squareart.py"), str(RAW), "300" if s["small_art"] else "500"],
                         self._counted("clean", ("art:",)))
        self._stream([core.PY, str(PIPELINE / "cleantitles.py"), str(RAW)])
        if preset:
            PRESET_FILE.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")
            self._stream([core.PY, str(PIPELINE / "presettags.py"), str(PRESET_FILE), str(RAW)])
        self._step("clean", "done", n_files)

        self._step("tag", "active", 0, n_files)
        self._set(now=f"Matching {core.plural(n_files, 'song')} on MusicBrainz")
        self._stream([core.PY, str(PIPELINE / "prunelib.py"), run_cfg])   # forget songs deleted from the folder
        beets_log: list[str] = []
        counting = self._counted("tag", (str(RAW),))

        def on_beets(line: str):
            beets_log.append(line)
            return counting(line)

        beets_ok = self._stream([core.PY, "-m", "beets", "-c", run_cfg, "import", "-q", "-s", str(RAW)], on_beets) == 0
        n_asis = sum(1 for ln in beets_log if "Importing as-is" in ln)
        dupes, current = [], None   # already in the library: beets leaves the extra copy in newdownloads
        for ln in beets_log:
            if ln.strip().startswith(str(RAW)):
                current = Path(ln.strip())
            elif "already in the library" in ln and current:
                dupes.append(current)
        for p in dupes:
            self._log(f"  Skipped, already in your library: {core.strip_id(p.stem)}")
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
        self._stream([core.PY, str(PIPELINE / "fixartists.py"), run_cfg])
        album_log: list[str] = []
        counting_albums = self._counted("albums", ("album:",))

        def on_albums(line: str):
            album_log.append(line)
            return counting_albums(line)

        self._stream([core.PY, str(PIPELINE / "fillalbums.py"), run_cfg], on_albums)
        self._step("albums", "done")

        n_new = max(n_files - len(dupes), 0)
        self._step("genres", "active", 0, n_new)
        self._set(now="Genres from Last.fm")
        self._stream([core.PY, str(PIPELINE / "fillgenres.py"), run_cfg], self._counted("genres", ("genre:",)))
        if s["replaygain"]:
            # beets doesn't always write the loudness values into files it couldn't match
            self._stream([core.PY, "-m", "beets", "-c", run_cfg, "write"])
        self._step("genres", "done")
        return {"files": n_files, "dupes": len(dupes), "asis": n_asis, "matched": n_matched,
                "albums": sum(1 for ln in album_log if 'album "' in ln)}

    def _worker(self, job: dict, queued: bool) -> None:
        result = "failed"
        try:
            s = core.load_settings()
            run_cfg = str(core.write_run_config(s))
            if queued:   # one log for the whole queue: don't wipe the download before this one
                core.runlog(f"\n{datetime.datetime.now():%Y-%m-%d %H:%M}  Next in the queue: {job['label']}")
            else:
                core.start_runlog(f"New window: {job['label']}")
            preset, plists, targets = None, None, list(job.get("urls") or [])
            if job["kind"] == "playlists":
                plan = playlistrun.check(self, job)
                plists, targets = plan["plists"], plan["targets"]
                if not targets and not core.raw_songs():   # like the classic window: nothing to download or tag
                    for k in ("download", "clean", "tag", "albums", "genres"):
                        self._step(k, "skipped")
                    if s["playlist_files"] and plists:
                        playlistrun.write_files(self, s, plists)
                    result = "done"
                    self._set(summary="Up to date: no new songs")
                    return
            get = []
            if job["kind"] == "album":
                get = self._one_track_per_video(self._find(job), job["tracks"])
                targets = [f"https://music.youtube.com/watch?v={v}" for _, v in get]
                for i, v in get:
                    self._song(lambda x, i=i: x["i"] == i, video=v)
            yt = {"songs": 0, "blocked": False, "dead": 0}
            if targets:
                yt = self._download(targets, s, known_only=job["kind"] == "playlists")
                if job["kind"] == "album" and not yt["blocked"]:
                    get = self._retry_age_gated(job, s, get, yt)
                if yt["blocked"]:
                    self._log("  YouTube is refusing downloads right now, so downloading stopped. "
                              "The songs that did download still get tagged.")
            else:
                self._step("download", "skipped")
            if job["kind"] == "album":
                t = job["tracks"]
                # no recording id (rare): it still downloads, beets tags it the usual way
                preset = {"release": job["release"], "tracks": [{
                    "video_id": v, "recording_id": t[i]["recording_id"], "title": t[i]["title"], "artist": t[i]["artist"],
                    "disc": t[i].get("disc"), "n": t[i].get("n")} for i, v in get if t[i].get("recording_id")]}
            core.clean_raw()
            self._check()
            # Always, like the classic window, even with nothing new: prunelib forgets deleted songs
            # and fillgenres backfills Last.fm tags for older songs on every run.
            tagged = self._tag(s, run_cfg, preset)
            if plists and s["playlist_files"]:
                playlistrun.write_files(self, s, plists)
            core.clean_raw()
            with self._lock:
                for x in self._state["songs"]:
                    if x["state"] == "downloaded":
                        x["state"] = "done"
            result = "done"
            self._set(summary=_summary(yt, tagged, self.status()["songs"]))
        except Stopped:
            result = "stopped"
            core.clean_raw()   # half-finished files; finished ones stay and are tagged next time
            self._set(summary="Stopped. Anything already downloaded is tagged on the next download.")
        except Exception as e:   # noqa: BLE001 - shown in the window and the log; the run must end cleanly
            core.runlog(f"!! new window download crashed: {type(e).__name__}: {e}\n")
            self._problem(f"{type(e).__name__}: {e}")
            self._set(summary="The download stopped with a problem.")
        finally:
            self._end(result)


def _first_step(key: str, name: str, job: dict, s: dict) -> dict:
    """A step as a job starts: "Find" is "Check" for playlists, links don't search, and only
    playlist updates write playlist files (when Preferences say so)."""
    playlists = job["kind"] == "playlists"
    skipped = (key == "find" and job["kind"] == "links") or (key == "lists" and not (playlists and s.get("playlist_files")))
    return {"key": key, "name": "Check" if key == "find" and playlists else name,
            "state": "skipped" if skipped else "waiting", "done": 0, "total": 0}


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
    parts = [f"{core.plural(yt['songs'], 'song')} downloaded"]
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
    if count["agegated"]:
        parts.append(f"{count['agegated']} age-restricted")
    reuploads = sum(1 for x in songs if x.get("note") == REUPLOAD_NOTE and x["state"] in ("done", "downloaded"))
    if reuploads:
        parts.append(f"{reuploads} from someone else's upload")
    if tagged:
        if tagged["dupes"]:
            parts.append(f"{tagged['dupes']} already in library")
        if tagged["matched"]:
            parts.append(f"{tagged['matched']} matched on MusicBrainz")
        if tagged["asis"]:
            parts.append(f"{tagged['asis']} kept YouTube's info")
    return ", ".join(parts)
