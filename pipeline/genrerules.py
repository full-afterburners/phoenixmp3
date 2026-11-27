# Genre settings for the new window (app.pyw): read them, count exactly what a change
# would do, save them, and re-file the library. Your choices are saved to my-genres.txt
# (the genre list) and my-genres.yaml (lastgenre source/family/moves), personal files
# that fillgenres.py prefers over the shipped genres.txt and config.yaml. Counting runs
# fillgenres.py --preview (kept Last.fm tags only, changes nothing) twice at once: with
# the saved settings and with the new ones. Re-filing runs fillgenres.py --refile as a
# background job whose progress the window polls.
import os
import re
import subprocess
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

import fillgenres as fg

PIPELINE = Path(__file__).resolve().parent
PYTHON = PIPELINE / "python" / "python.exe"
SOURCES = ("track", "album", "artist")
FAMILIES = ("history", "sound")
MAX_NAME = 60
CREATE_NO_WINDOW = 0x08000000
ERROR_LINE = re.compile(r"^\w+(Error|Exception): ")   # a Python error, like gui.pyw's _only_problems()


class RulesError(ValueError):
    """The page sent settings that can't be saved; the message says why."""


def child_env() -> dict:
    """The environment every pipeline step gets (same as child_env in gui.pyw)."""
    env = os.environ.copy()
    env.update(PYTHONPATH=str(PIPELINE / "libs"), BEETSDIR=str(PIPELINE), FPCALC=str(PIPELINE / "bin" / "fpcalc.exe"),
               PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PIP_USER="0")
    env["PATH"] = str(PIPELINE / "bin") + os.pathsep + env.get("PATH", "")
    return env


def _lastgenre(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    return data.get("lastgenre") or {}


def current_rules() -> dict:
    """The genre settings in use: list, source, family and moves (a parent of None means top level)."""
    lg = {**_lastgenre(PIPELINE / "config.yaml"), **_lastgenre(PIPELINE / fg.MY_RULES)}
    moves = {str(g).lower(): (None if str(p).lower() == "top" else str(p).lower()) for g, p in (lg.get("moves") or {}).items()}
    return {"list": sorted(fg.allowed()), "source": lg.get("source", "album"), "family": lg.get("family", "sound"),
            "moves": moves}


def _name(value, what: str) -> str:
    # isprintable() also rules out the other characters splitlines() breaks on, so a name can't become two
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME or not value.isprintable() or "#" in value:
        raise RulesError(f"{what} isn't a usable genre name: {value!r}")
    return value.strip().lower()


def clean_rules(rules) -> dict:
    """A checked copy of settings sent by the page."""
    if not isinstance(rules, dict):
        raise RulesError("settings must be an object")
    picked = sorted({_name(g, "a listed genre") for g in rules.get("list") or []})
    if not picked:
        raise RulesError("the genre list can't be empty: songs would get no genre at all")
    if rules.get("source") not in SOURCES:
        raise RulesError(f"source must be one of {', '.join(SOURCES)}")
    if rules.get("family") not in FAMILIES:
        raise RulesError(f"grouping must be one of {', '.join(FAMILIES)}")
    raw_moves = rules.get("moves") or {}
    if not isinstance(raw_moves, dict):
        raise RulesError("moves must be an object")
    moves = {_name(g, "a moved genre"): (None if p is None else _name(p, "a new parent")) for g, p in raw_moves.items()}
    for g, p in moves.items():
        if p == g:
            raise RulesError(f"{g} can't be moved under itself")
    return {"list": picked, "source": rules["source"], "family": rules["family"], "moves": moves}


def _write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)   # all or nothing, so a crash can't leave half a file


def write_rules(folder: Path, rules: dict) -> None:
    _write(folder / fg.MY_LIST, "# Your genre list, saved by phoenixmp3 (it wins over genres.txt).\n"
                                "# Last.fm tags are mapped up to one of these.\n" + "\n".join(rules["list"]) + "\n")
    lastgenre = {"source": rules["source"], "family": rules["family"],
                 "moves": {g: (p or "top") for g, p in sorted(rules["moves"].items())}}
    _write(folder / fg.MY_RULES, "# Your genre settings, saved by phoenixmp3 (they win over config.yaml).\n"
                                 + yaml.safe_dump({"lastgenre": lastgenre}, allow_unicode=True, sort_keys=False))


def _preview(rules_dir: Path | None) -> tuple[dict, int]:
    """{song id: (genre now, genre after)} for songs a re-file would change, and how many lack kept tags."""
    cmd = [str(PYTHON), str(PIPELINE / "fillgenres.py"), "--preview"] + (["--rules", str(rules_dir)] if rules_dir else [])
    run = subprocess.run(cmd, cwd=PIPELINE, env=child_env(), capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=300, creationflags=CREATE_NO_WINDOW)
    if run.returncode != 0:
        raise RuntimeError("counting failed: " + (run.stderr.strip().splitlines() or ["no output"])[-1])
    moves, untagged = {}, 0
    for line in run.stdout.splitlines():
        line = line.lstrip()   # not strip(): "genre after" can be empty, which ends the line with a tab
        if line.startswith("move: "):
            sid, now, then = (line[6:].split("\t") + ["", ""])[:3]
            moves[int(sid)] = (now, then)
        elif line.startswith("preview: "):
            untagged = int(line.rsplit(",", 1)[1].split()[0])
    return moves, untagged


def preview(rules) -> dict:
    """Exactly what saving would do on a re-file: songs your changes move (with the commonest
    moves), songs any re-file would move, and songs with no kept tags yet (not counted)."""
    rules = clean_rules(rules)
    with tempfile.TemporaryDirectory(prefix="phoenix-rules-") as tmp:
        write_rules(Path(tmp), rules)
        with ThreadPoolExecutor(max_workers=2) as pool:
            saved_job, new_job = pool.submit(_preview, None), pool.submit(_preview, Path(tmp))
            saved, _ = saved_job.result()
            new, untagged = new_job.result()
    # A song not listed keeps its genre under those rules; compare what each set of rules would give it.
    by_edits = Counter()
    for sid in set(saved) | set(new):
        before = saved[sid][1] if sid in saved else (new[sid][0] if sid in new else None)
        after = new[sid][1] if sid in new else saved[sid][0]
        if before != after:
            by_edits[(before or "no genre", after or "no genre")] += 1
    return {"edits": sum(by_edits.values()), "refile": len(new), "untagged": untagged,
            "top": [{"from": a, "to": b, "n": n} for (a, b), n in by_edits.most_common(5)]}


class Refile:
    """fillgenres.py --refile running in the background, one at a time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._state = {"running": False, "done": 0, "kept": 0, "errors": [], "code": None}

    def running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def start(self) -> None:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("a re-file is already running")
            # Popen first: if it fails, the job must not be left marked as running forever
            self._proc = subprocess.Popen([str(PYTHON), str(PIPELINE / "fillgenres.py"), "--refile"], cwd=PIPELINE,
                                          env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                          encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
            self._state = {"running": True, "done": 0, "kept": 0, "errors": [], "code": None}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        proc = self._proc
        for line in proc.stdout:
            line = line.strip()
            with self._lock:
                song_line = line.startswith("genre:")
                if song_line and "kept hand-set" in line:
                    self._state["kept"] += 1
                elif song_line and not line.startswith("genre: couldn't"):
                    self._state["done"] += 1
                # problems only, worded the way gui.pyw's _only_problems() looks for them (not song titles)
                if line.startswith(("genre: couldn't", "lookup failed", "Traceback")) or ERROR_LINE.match(line):
                    self._state["errors"] = (self._state["errors"] + [line])[-5:]
        code = proc.wait()
        with self._lock:
            self._state.update(running=False, code=code)

    def status(self) -> dict:
        with self._lock:
            return {**self._state, "errors": list(self._state["errors"])}


SAVE_LOCK = threading.Lock()   # one save at a time: the two files are written as a pair


def save(rules, refile_job: Refile, refile: bool = False) -> dict:
    """Save the settings; with refile=True, also start re-filing the library with them.
    Refuses while a re-file is running: it loaded the old settings and would mix the two."""
    rules = clean_rules(rules)
    with SAVE_LOCK:
        if refile_job.running():
            raise RulesError("genres are still being re-filed; save again when that's finished")
        write_rules(PIPELINE, rules)
        if refile:
            refile_job.start()
    return current_rules()
