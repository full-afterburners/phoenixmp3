# Keeping the download tools current, so nobody has to swap folders by hand.
#  - yt-dlp: YouTube changes often, and an old yt-dlp is the usual reason downloads fail.
#    The newest release's source (yt-dlp.tar.gz) is checked against the SHA2-256SUMS file
#    yt-dlp publishes, tried out (--version) from a scratch folder, then swapped in for
#    yt-dlp-master/yt_dlp. The old copy stays as yt_dlp.previous; if the new one doesn't
#    run after the swap, the old one goes straight back.
#  - Deno (bin/deno.exe), which yt-dlp uses for YouTube's checks: the same, from Deno's
#    releases and their .sha256sum, but only within the same major version (a new major
#    version could stop working with yt-dlp).
#  - phoenixmp3 itself is only checked: a newer release on GitHub is shown with a link to
#    its page. version.txt (written by build-release.ps1) says which release this is; a
#    working copy has none and is never told to update.
# GitHub's public API is asked at most once a day unless the user presses Check Now; no
# account and nothing about the user is sent. Installing never runs during a download:
# app.pyw reserves the update first (reserve()) and refuses downloads while it runs.
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile

import core

PIPELINE = core.PIPELINE
YTDLP_DIR = PIPELINE / "yt-dlp-master"
YTDLP_PKG = YTDLP_DIR / "yt_dlp"
DENO = core.BIN / "deno.exe"
VERSION_FILE = PIPELINE / "version.txt"
STATE_FILE = PIPELINE / "cache" / "updates.json"
WORK = PIPELINE / "cache" / "updates"
APP_REPO = "full-afterburners/phoenixmp3"
RELEASE_PAGE = f"https://github.com/{APP_REPO}/releases/"
DENO_ASSET = "deno-x86_64-pc-windows-msvc.zip"
CHECK_EVERY = 20 * 3600          # seconds between automatic checks
TIMEOUT = 60
HEADERS = {"User-Agent": "phoenixmp3-updater", "Accept": "application/vnd.github+json"}
TOOLS = ("ytdlp", "deno")

_state_lock = threading.Lock()
_installing = threading.Event()   # set while an install runs: downloads must wait


# ---------- versions ----------

def ytdlp_version(pkg=None) -> str | None:
    try:
        m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", ((pkg or YTDLP_PKG) / "version.py").read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def deno_version(exe=None) -> str | None:
    try:
        out = subprocess.run([str(exe or DENO), "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=core.NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"deno (\S+)", out or "")
    return m.group(1) if m else None


def app_version() -> str | None:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _parts(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def newer(latest: str | None, have: str | None) -> bool:
    return bool(latest and have and _parts(latest) > _parts(have))


# ---------- state (cache/updates.json) ----------

def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def auto_enabled() -> bool:
    return _load().get("auto", True)


def set_auto(on: bool) -> dict:
    with _state_lock:
        state = _load()
        state["auto"] = bool(on)
        _save(state)
    return status()


# ---------- GitHub ----------

def _get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT) as r:
        return r.read()


def _latest(repo: str) -> dict:
    """{"tag", "url", "assets": {name: download url}} of a repo's newest release."""
    d = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))
    return {"tag": d.get("tag_name") or "", "url": d.get("html_url") or "",
            "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets") or [] if a.get("name")}}


def check(force: bool = False) -> dict:
    """Ask GitHub for the newest releases (at most once per CHECK_EVERY unless forced)."""
    with _state_lock:
        state = _load()
        if not force and time.time() - state.get("checked", 0) < CHECK_EVERY:
            return status()
        found, errors = {}, []
        for key, repo in (("ytdlp", "yt-dlp/yt-dlp"), ("deno", "denoland/deno"), ("app", APP_REPO)):
            try:
                rel = _latest(repo)
                found[key] = {"latest": rel["tag"].lstrip("v"), "url": rel["url"]}
            except (OSError, ValueError, KeyError) as e:
                errors.append(f"{repo}: {e}")
        state.update(found)
        state["checked"] = time.time()
        state["error"] = "; ".join(errors)
        _save(state)
    return status()


def status() -> dict:
    """What's installed, what's out, and what can be updated, for Preferences."""
    state = _load()
    have = {"ytdlp": ytdlp_version(), "deno": deno_version(), "app": app_version()}
    out = {"checked": state.get("checked"), "error": state.get("error") or "", "auto": state.get("auto", True),
           "installing": _installing.is_set()}
    for key in ("ytdlp", "deno", "app"):
        latest = (state.get(key) or {}).get("latest")
        can = newer(latest, have[key])
        if key == "deno" and can:   # same major version only
            can = _parts(latest)[:1] == _parts(have[key])[:1]
        out[key] = {"have": have[key], "latest": latest, "newer": can}
    url = (state.get("app") or {}).get("url") or ""
    out["app"]["url"] = url if url.startswith(RELEASE_PAGE) else RELEASE_PAGE
    return out


# ---------- installing ----------

def reserve() -> bool:
    """Claim the one install slot. The caller (app.pyw) does this under its download lock, so a
    download can't start in between; install() frees it."""
    if _installing.is_set():
        return False
    _installing.set()
    return True


def installing() -> bool:
    return _installing.is_set()


def _verify(data: bytes, expected: str, what: str) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got.lower() != expected.lower():
        raise ValueError(f"the downloaded {what} didn't match its published checksum, so it wasn't used")


def _runs(cmd: list[str]) -> bool:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=core.child_env(), creationflags=core.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and bool(re.match(r"\S*\d", (p.stdout or "").strip()))


def _fresh_work() -> None:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)


def _install_ytdlp() -> str:
    rel = _latest("yt-dlp/yt-dlp")
    sums, tar_url = rel["assets"].get("SHA2-256SUMS"), rel["assets"].get("yt-dlp.tar.gz")
    if not sums or not tar_url:
        raise ValueError("yt-dlp's newest release doesn't have its source download")
    line = next((ln for ln in _get(sums).decode().splitlines() if ln.strip().endswith(" yt-dlp.tar.gz")), "")
    data = _get(tar_url)
    _verify(data, line.split()[0] if line else "", "yt-dlp")
    _fresh_work()
    (WORK / "yt-dlp.tar.gz").write_bytes(data)
    with tarfile.open(WORK / "yt-dlp.tar.gz") as t:
        t.extractall(WORK / "new", filter="data")   # refuses paths that would land outside the folder
    new = WORK / "new" / "yt-dlp" / "yt_dlp"
    if not _runs([core.PY, str(new / "__main__.py"), "--version"]):
        raise ValueError("the new yt-dlp didn't start, so the old one was kept")
    backup = YTDLP_DIR / "yt_dlp.previous"
    shutil.rmtree(backup, ignore_errors=True)
    os.rename(YTDLP_PKG, backup)
    try:
        os.rename(new, YTDLP_PKG)
    except OSError:
        os.rename(backup, YTDLP_PKG)
        raise
    if not _runs([core.PY, str(core.YTDLP), "--version"]):   # the real place, the way downloads run it
        shutil.rmtree(YTDLP_PKG, ignore_errors=True)
        os.rename(backup, YTDLP_PKG)
        raise ValueError("the new yt-dlp didn't run in place, so the old one was put back")
    return ytdlp_version() or rel["tag"]


def _install_deno() -> str:
    have = deno_version()
    rel = _latest("denoland/deno")
    latest = rel["tag"].lstrip("v")
    if _parts(latest)[:1] != _parts(have)[:1]:
        raise ValueError(f"Deno {latest} is a new major version; it's left for a phoenixmp3 release")
    zip_url, sum_url = rel["assets"].get(DENO_ASSET), rel["assets"].get(DENO_ASSET + ".sha256sum")
    if not zip_url or not sum_url:
        raise ValueError("Deno's newest release doesn't have the Windows download")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", _get(sum_url).decode(errors="replace"))
    data = _get(zip_url)
    _verify(data, m.group(1) if m else "", "Deno")
    _fresh_work()
    (WORK / "deno.zip").write_bytes(data)
    with zipfile.ZipFile(WORK / "deno.zip") as z:
        if "deno.exe" not in z.namelist():
            raise ValueError("Deno's download didn't contain deno.exe")
        z.extract("deno.exe", WORK)
    if not deno_version(WORK / "deno.exe"):
        raise ValueError("the new Deno didn't start, so the old one was kept")
    backup = DENO.with_name("deno.exe.previous")
    backup.unlink(missing_ok=True)
    os.rename(DENO, backup)
    try:
        os.rename(WORK / "deno.exe", DENO)
    except OSError:
        os.rename(backup, DENO)
        raise
    if not deno_version():
        DENO.unlink(missing_ok=True)
        os.rename(backup, DENO)
        raise ValueError("the new Deno didn't run in place, so the old one was put back")
    return deno_version() or latest


def install(tool: str) -> dict:
    """Install the newest yt-dlp or Deno. The caller must have reserve()d; this frees it."""
    try:
        if tool not in TOOLS:
            raise ValueError("unknown tool")
        version = (_install_ytdlp if tool == "ytdlp" else _install_deno)()
        core.runlog(f"updated {tool} to {version}")
        return status()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
        _installing.clear()


def pending() -> list[str]:
    """Tools with a newer version (from the last check) that can be installed."""
    st = status()
    return [t for t in TOOLS if st[t]["newer"]]
