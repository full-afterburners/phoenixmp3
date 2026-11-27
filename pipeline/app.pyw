# The new phoenixmp3 window (work in progress; gui.pyw is still the app until this one
# can do everything). It's a web page (ui/index.html) shown in a native window through
# pywebview, which uses Microsoft Edge WebView2 (built into Windows 10/11). The page asks
# Python for data through window.pywebview.api (the Api class below); appdata.py does
# the reading, read-only. The page is served from a private 127.0.0.1 port.
#
#   pythonw app.pyw [--debug-port <port>]   (the port opens WebView2's DevTools protocol, for UI tests)
import functools
import http.server
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
LIBS = PIPELINE / "libs"
BIN = PIPELINE / "bin"
UI = PIPELINE / "ui"
LOG = PIPELINE / "app.log"
UI_PORT = 47613   # arbitrary, out of the common ranges

# Same environment the pipeline steps get (see child_env in gui.pyw), set before beets is imported.
sys.path[:0] = [str(LIBS), str(PIPELINE)]
os.environ["BEETSDIR"] = str(PIPELINE)
os.environ["FPCALC"] = str(BIN / "fpcalc.exe")
os.environ["PATH"] = str(BIN) + os.pathsep + os.environ.get("PATH", "")
os.environ["PYTHONNOUSERSITE"] = "1"
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

logging.basicConfig(filename=LOG, filemode="w", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")


def open_classic() -> None:
    subprocess.Popen([str(PIPELINE / "python" / "pythonw.exe"), str(PIPELINE / "gui.pyw")], cwd=str(PIPELINE.parent))


try:
    import webview  # noqa: E402  (needs libs on sys.path)

    import appdata  # noqa: E402
    import appsettings  # noqa: E402
    import downloadrun  # noqa: E402
    import genrerules  # noqa: E402
    import musicinfo  # noqa: E402
    import myplaylists  # noqa: E402
except Exception:
    # pythonw has no console: without this a broken install would just show nothing at all
    log.exception("the new window's parts didn't load; opening the classic window")
    open_classic()
    sys.exit(1)


class Api:
    """Everything the page can ask Python for. Public methods become window.pywebview.api.*;
    each runs on its own thread, and an exception rejects the page's promise with its message."""

    def __init__(self) -> None:
        self._refile = genrerules.Refile()   # underscore: not exposed to the page
        self._downloads = downloadrun.DownloadRun()
        self._busy = threading.Lock()   # a download and a re-file both change the library: one at a time

    def preview_genres(self, rules: dict) -> dict:
        """Exactly how many songs these (unsaved) genre settings would move."""
        return _logged("counting genre changes", genrerules.preview, rules)

    def save_genres(self, rules: dict, refile: bool) -> dict:
        """Save the genre settings; refile=True also re-files the library in the background."""
        with self._busy:
            if refile is True and self._downloads.running():
                raise genrerules.RulesError("a download is running; save again when it's finished")
            return _logged("saving genre settings", genrerules.save, rules, self._refile, refile is True)

    def start_download(self, job: dict) -> dict:
        """job: {"kind": "links", "urls": [...]}, {"kind": "album", "release_id", "picks": [...]} or
        {"kind": "playlists", "urls": [...]}. Queued behind a running download ("queued": true)."""
        checked = _logged("checking a download", downloadrun.parse_job, job)   # may ask MusicBrainz: not under the lock
        with self._busy:
            if self._refile.running():
                raise ValueError("genres are being re-filed; download when that's finished")
            return _logged("starting a download", self._downloads.start, checked)

    def download_status(self) -> dict:
        return self._downloads.status()

    def stop_download(self, everything: bool = False) -> bool:
        """Stop the running download; the next queued one starts unless everything is True."""
        return self._downloads.stop(everything is True)

    def library_album(self, name: str, artist: str) -> dict:
        """How many tracks an album in your library has, from MusicBrainz ({} when it doesn't know it)."""
        return _logged("looking up an album's track count", musicinfo.library_album, name, artist)

    def unqueue_download(self, qid: int) -> bool:
        """Take a download that hasn't started yet out of the queue."""
        return self._downloads.unqueue(qid)

    def refile_library(self) -> None:
        """Re-file every song's genre with the settings in use (Preferences > Re-file my library)."""
        with self._busy:
            if self._downloads.running():
                raise ValueError("a download is running; re-file when it's finished")
            if self._refile.running():
                raise ValueError("genres are already being re-filed")
            _logged("starting a re-file", self._refile.start)

    # ---------- Preferences ----------

    def get_settings(self) -> dict:
        return _logged("reading settings", appsettings.get)

    def _not_downloading(self) -> None:
        # Like the classic window, which locks Settings while it works: the page would show
        # settings the running download isn't using.
        if self._downloads.running():
            raise ValueError("a download is running; change settings when it's finished")

    def save_settings(self, changes: dict) -> dict:
        self._not_downloading()
        return _logged("saving settings", appsettings.save, changes)

    def set_theme(self, theme: str) -> dict:
        return _logged("saving the theme", appsettings.set_theme, theme)

    def choose_folder(self) -> dict | None:
        """Ask for the music folder with Windows' folder picker; None if cancelled."""
        self._not_downloading()
        start = appsettings.get()["folder"]
        picked = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not picked:
            return None
        return _logged("changing the music folder", appsettings.set_folder, picked[0])

    # ---------- My Playlists ----------

    def add_playlist(self, url: str, name: str) -> list:
        return _logged("adding a playlist", myplaylists.add, url, name)

    def remove_playlist(self, url: str) -> list:
        return _logged("removing a playlist", myplaylists.remove, url)

    def check_playlist(self, url: str) -> dict:
        """New songs in one saved playlist (yt-dlp lists it; nothing downloads)."""
        return _logged("checking a playlist", myplaylists.check, url)

    def refile_status(self) -> dict:
        return self._refile.status()

    def load(self, name: str) -> dict:
        loaders = {"data": appdata.library, "genres": appdata.genres}
        if name not in loaders:
            raise ValueError(f"unknown data set: {name!r}")
        try:
            return loaders[name]()
        except Exception:
            log.exception("loading %s failed", name)
            raise

    def open_classic(self) -> None:
        """Open the old window (gui.pyw), which still does the downloading for now."""
        open_classic()

    def genre_albums(self, tag: str) -> list:
        if not isinstance(tag, str) or not tag.strip() or len(tag) > 100:
            raise ValueError("bad genre name")
        return _logged(f"Last.fm albums for {tag}", appdata.genre_albums, tag.strip().lower())

    def search(self, kind: str, text: str, artist: str | None = None) -> list:
        """MusicBrainz search: kind is albums, songs or artists."""
        return _logged(f"searching {kind}", musicinfo.search, kind, text, artist)

    def artist_profile(self, mbid: str) -> dict:
        return _logged("loading an artist profile", musicinfo.artist, mbid)

    def album_page(self, group_id: str) -> dict:
        return _logged("loading an album", musicinfo.album, group_id)

    def release_tracks(self, release_id: str) -> dict:
        return _logged("loading an edition", musicinfo.release, release_id)


def _logged(what: str, fn, *args):
    """Run fn, logging unexpected failures; settings the page got wrong (RulesError) aren't logged as crashes."""
    try:
        return fn(*args)
    except genrerules.RulesError:
        raise
    except (OSError, ValueError) as e:   # offline, a service said no, bad input: the page shows it
        log.warning("%s failed: %s", what, e)
        raise
    except Exception:
        log.exception("%s failed", what)
        raise


class ExclusiveServer(http.server.ThreadingHTTPServer):
    # Windows lets a second program bind a port marked reuse-address, so the "port taken" fallback
    # would never happen and two windows could end up sharing one address.
    allow_reuse_address = False


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):   # pythonw has no console to log to
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # always show the current page after an update
        super().end_headers()


def serve_ui() -> int:
    """Serve ui/ on 127.0.0.1. Always the same port when it's free, because the page's saved
    preferences (localStorage) belong to its address; a random port would forget them each start."""
    handler = functools.partial(QuietHandler, directory=str(UI))
    try:
        server = ExclusiveServer(("127.0.0.1", UI_PORT), handler)
    except OSError:
        log.warning("port %s is taken; preferences saved in the page won't carry over this time", UI_PORT)
        server = ExclusiveServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def main() -> None:
    args = sys.argv[1:]
    if "--debug-port" in args:
        port = int(args[args.index("--debug-port") + 1])
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"
    url = f"http://127.0.0.1:{serve_ui()}/index.html?desktop"
    log.info("serving %s", url)
    webview.create_window("phoenixmp3", url, js_api=Api(), width=1280, height=820, min_size=(900, 600),
                          background_color="#1e1e20")
    # Its own taskbar entry with the app's icon, instead of being grouped under Python's.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("phoenixmp3.app")
    except (AttributeError, OSError):
        log.warning("couldn't set the taskbar id; the taskbar may show Python's icon")
    # private_mode off + a storage folder so the page remembers things like the theme between runs.
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(PIPELINE / "cache" / "webview"),
                      icon=str(PIPELINE / "icon.ico"))
    except Exception:
        # Most likely no WebView2 on this PC: open the classic window instead of nothing.
        log.exception("the new window couldn't start; opening the classic one")
        open_classic()


if __name__ == "__main__":
    main()
