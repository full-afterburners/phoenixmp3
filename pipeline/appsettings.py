# Preferences for the new app window (app.pyw). Download settings live in settings.json
# and go through the classic window's own load_settings()/save_settings(), so both
# windows always agree (gui.pyw keeps only the keys it knows, with its defaults).
# Settings only the new window has (the theme) go in app-prefs.json, which the classic
# window never rewrites. Both are personal files: gitignored and kept out of the release.
import functools
import json
import os
import threading
from pathlib import Path

import gui

APP_PREFS = gui.PIPELINE / "app-prefs.json"
THEMES = ("dark", "light", "system")
CHOICES = {"format": ("aac", "mp3"), "layout": ("flat", "folders")}
SWITCHES = ("square_art", "small_art", "playlist_files", "replaygain")
MAX_PAUSE = 30
_lock = threading.Lock()   # the window calls in from several threads: one read-change-save at a time


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper


def _app_prefs() -> dict:
    try:
        data = json.loads(APP_PREFS.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def get() -> dict:
    s = gui.load_settings()
    return {"settings": s, "folder": str(gui.out_dir(s)), "default_folder": not s["output_dir"],
            # None until the window saves one (it then sends the theme it remembered itself)
            "theme": _app_prefs().get("theme") if _app_prefs().get("theme") in THEMES else None}


@_locked
def save(changes) -> dict:
    """Checked changes to settings.json; anything not sent stays as it was."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("nothing to save")
    s = gui.load_settings()
    for key, value in changes.items():
        if key in CHOICES and value in CHOICES[key]:
            s[key] = value
        elif key in SWITCHES and isinstance(value, bool):
            s[key] = value
        elif key == "pause" and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_PAUSE:
            s[key] = value
        elif key == "output_dir" and value == "":   # back to the default folder
            s[key] = ""
        else:
            raise ValueError(f"can't set {key} to {value!r}")
    gui.save_settings(s)
    return get()


@_locked
def set_folder(path: str) -> dict:
    """The music folder picked in the folder dialog. Songs already downloaded stay where they are."""
    p = Path(path)
    if not p.is_absolute() or not p.is_dir():
        raise ValueError("that folder doesn't exist")
    s = gui.load_settings()
    s["output_dir"] = gui.store_dir(p)
    gui.save_settings(s)
    return get()


@_locked
def set_theme(theme) -> dict:
    if theme not in THEMES:
        raise ValueError("unknown theme")
    prefs = {**_app_prefs(), "theme": theme}
    tmp = APP_PREFS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    os.replace(tmp, APP_PREFS)
    return get()
