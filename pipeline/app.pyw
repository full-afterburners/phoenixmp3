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

import webview  # noqa: E402  (needs libs on sys.path)

import appdata  # noqa: E402
import genrerules  # noqa: E402

logging.basicConfig(filename=LOG, filemode="w", level=logging.INFO, encoding="utf-8",
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("app")


class Api:
    """Everything the page can ask Python for. Public methods become window.pywebview.api.*;
    each runs on its own thread, and an exception rejects the page's promise with its message."""

    def __init__(self) -> None:
        self._refile = genrerules.Refile()   # underscore: not exposed to the page

    def preview_genres(self, rules: dict) -> dict:
        """Exactly how many songs these (unsaved) genre settings would move."""
        return _logged("counting genre changes", genrerules.preview, rules)

    def save_genres(self, rules: dict, refile: bool) -> dict:
        """Save the genre settings; refile=True also re-files the library in the background."""
        return _logged("saving genre settings", genrerules.save, rules, self._refile if refile is True else None)

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
        try:
            return appdata.genre_albums(tag.strip().lower())
        except Exception:
            log.exception("Last.fm albums for %s failed", tag)
            raise


def _logged(what: str, fn, *args):
    """Run fn, logging unexpected failures; settings the page got wrong (RulesError) aren't logged as crashes."""
    try:
        return fn(*args)
    except genrerules.RulesError:
        raise
    except Exception:
        log.exception("%s failed", what)
        raise


def open_classic() -> None:
    subprocess.Popen([str(PIPELINE / "python" / "pythonw.exe"), str(PIPELINE / "gui.pyw")], cwd=str(PIPELINE.parent))


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
        server = http.server.ThreadingHTTPServer(("127.0.0.1", UI_PORT), handler)
    except OSError:
        log.warning("port %s is taken; preferences saved in the page won't carry over this time", UI_PORT)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
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
    # private_mode off + a storage folder so the page remembers things like the theme between runs.
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(PIPELINE / "cache" / "webview"))
    except Exception:
        # Most likely no WebView2 on this PC: open the classic window instead of nothing.
        log.exception("the new window couldn't start; opening the classic one")
        open_classic()


if __name__ == "__main__":
    main()
