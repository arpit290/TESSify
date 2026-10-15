"""TESS light-curve / target-pixel project manager (needs Python 3.9+).

Typical workflow:

    project = Project()
    project.create("my_project")                  # or: project.restore("my_project")
    project.download("lc", "bulk_download.sh", 100)
    project.process(100)
    project.finalise(100)                         # Down = mark, Up = unmark, Esc = quit
    project.saveresults("candidates.json")

Every stage is resumable: progress lives in <project>/config.json.

Note: if you call process(..., workers=N) with N > 1 on Windows/macOS, put your
script's top-level code under ``if __name__ == "__main__":`` (standard rule for
multiprocessing).
"""
from __future__ import annotations

import gc
import json
import os
import re
import shlex
import subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, fields
from functools import lru_cache, partial, wraps
from pathlib import Path
from typing import Callable, Iterable, Iterator

try:  # the viewer is optional, so headless machines can still download and process
    import tkinter as tk
    from PIL import Image, ImageTk
except ImportError:
    tk = None

KIND_DIRS = {"lc": "LightCurves", "tp": "TargetPixels"}
GRAPHS_DIR = "Graphs"
CONFIG_NAME = "config.json"
IMAGE_EXT = ".jpeg"
SAVE_EVERY = 25  # write config.json after this many completed files (and on exit)
TIC_PATTERN = re.compile(r"-s\d{4}-(\d{16})-")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def image_name(filename: str) -> str:
    """'tess...-s0014-...-s_lc.fits' -> 'tess...-s0014-...-s_lc.jpeg'."""
    return Path(filename).stem + IMAGE_EXT


def tic_id(name: str) -> str | None:
    """'tess...-s0014-0000000261136679-0150-s_lc.jpeg' -> 'TIC 261136679'."""
    match = TIC_PATTERN.search(name)
    if match is None:
        return None
    return "TIC " + (match.group(1).lstrip("0") or "0")


def read_commands(script_path: str | Path, limit: int) -> dict[str, list[str]]:
    """Parse the first `limit` curl commands of a MAST script into {output_file: argv}."""
    commands: dict[str, list[str]] = {}
    with open(script_path, "r", encoding="utf-8") as script:
        for line in script:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if len(commands) == limit:
                break
            argv = shlex.split(line)
            commands[_output_file(argv)] = argv
    return commands


def _output_file(argv: list[str]) -> str:
    """Return the file name given to curl's -o / --output flag."""
    for flag in ("-o", "--output"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            return Path(argv[argv.index(flag) + 1]).name
    raise ValueError(f"No output file (-o) found in command: {' '.join(argv)}")


def _write_json_atomically(path: Path, data: dict) -> None:
    """Write via a temp file so a crash never leaves a half-written config."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Workers (module-level so they can be sent to other processes)
# --------------------------------------------------------------------------- #
def _download(filename: str, commands: dict[str, list[str]], cwd: Path) -> None:
    result = subprocess.run(commands[filename], cwd=cwd)
    if result.returncode != 0 or not (cwd / filename).is_file():
        raise RuntimeError(f"download exited with code {result.returncode}")


def _render(filename: str, kind: str, source_dir: Path, graphs_dir: Path) -> None:
    """Clean one FITS file and save its plot."""
    import matplotlib
    matplotlib.use("Agg")  # no GUI windows, no clash with the Tk viewer
    import matplotlib.pyplot as plt
    import lightkurve as lk

    data = lk.read(source_dir / filename)
    if kind == "tp":
        data = data.to_lightcurve(aperture_mask="all")
    lightcurve = data.remove_nans().flatten()

    fig, ax = plt.subplots()
    try:
        lightcurve.plot(ax=ax)
        fig.savefig(graphs_dir / image_name(filename), bbox_inches="tight")
    finally:
        plt.close(fig)
        del data, lightcurve
        gc.collect()


def _run_jobs(job: Callable[[str], None], items: Iterable[str],
              workers: int, executor_cls) -> Iterator[tuple[str, Exception | None]]:
    """Run job(item) for every item, yielding (item, error_or_None) as each one finishes."""
    if workers <= 1:  # inline: simpler tracebacks, no pool start-up cost
        for item in items:
            try:
                job(item)
                yield item, None
            except Exception as error:
                yield item, error
        return

    pool = executor_cls(max_workers=workers)
    try:
        futures = {pool.submit(job, item): item for item in items}
        for future in as_completed(futures):
            yield futures[future], future.exception()
    finally:  # on Ctrl-C: let running jobs finish, drop the queued ones
        pool.shutdown(wait=True, cancel_futures=True)


# --------------------------------------------------------------------------- #
# Project state (saved as config.json)
# --------------------------------------------------------------------------- #
@dataclass
class State:
    name: str
    kind: str | None = None                 # "lc" or "tp"
    script: str | None = None
    downloaded: list[str] = field(default_factory=list)       # FITS file names
    download_failed: list[str] = field(default_factory=list)
    processed: list[str] = field(default_factory=list)        # image file names
    process_failed: list[str] = field(default_factory=list)
    reviewed: int = 0                       # how many processed images were viewed
    marked: list[str] = field(default_factory=list)           # images flagged in the viewer

    @classmethod
    def load(cls, path: Path) -> State:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "Download_Progress" in data:  # config written by the older versions
            data = cls._from_legacy(data)
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    @staticmethod
    def _from_legacy(data: dict) -> dict:
        download = data.get("Download_Progress", {})
        process = data.get("Processing_Progress", {})
        finalise = data.get("Finalising_Progress", {})
        return {
            "name": data.get("Name", ""),
            "kind": download.get("Type"),
            "script": download.get("Script"),
            "downloaded": download.get("List", []),
            "download_failed": download.get("Failed", []),
            "processed": process.get("List", []),
            "process_failed": process.get("Failed", []),
            "reviewed": finalise.get("Finalised", 0),
            "marked": finalise.get("List", []),
        }


# --------------------------------------------------------------------------- #
# Image viewer
# --------------------------------------------------------------------------- #
class ImageReviewer:
    """Tk window for stepping through plots.

    Right/Left: navigate    Down: mark    Up: unmark    Esc or close: finish
    """

    def __init__(self, graphs_dir: Path, images: list[str],
                 marked: list[str], on_change: Callable[[], None]):
        if tk is None:
            raise RuntimeError("The viewer needs tkinter and Pillow (pip install pillow).")
        self.graphs_dir = graphs_dir
        self.images = images
        self.marked = marked          # shared with the project state, edited in place
        self.on_change = on_change
        self.index = 0
        self.furthest = 0

        self.root = tk.Tk()
        self.max_size = (int(self.root.winfo_screenwidth() * 0.9),
                         int(self.root.winfo_screenheight() * 0.85))
        self.label = tk.Label(self.root)
        self.label.pack()
        self._load = lru_cache(maxsize=8)(self._read_photo)  # cheap back-and-forth browsing

        keys = {
            "Right": lambda: self._move(+1),
            "Left": lambda: self._move(-1),
            "Down": self._mark,
            "Up": self._unmark,
            "Escape": self.root.destroy,
        }
        self.root.bind("<Key>", lambda event: keys.get(event.keysym, lambda: None)())

    def run(self) -> int:
        """Block until the window closes; return how many images were reached."""
        self._show()
        self.root.mainloop()
        return self.furthest + 1

    def _read_photo(self, name: str):
        with Image.open(self.graphs_dir / name) as img:
            img.thumbnail(self.max_size)  # only ever shrinks
            return ImageTk.PhotoImage(img)

    def _show(self) -> None:
        name = self.images[self.index]
        flag = "  [MARKED]" if name in self.marked else ""
        self.root.title(f"{self.index + 1}/{len(self.images)}  {name}{flag}")
        try:
            photo = self._load(name)
        except OSError as error:
            self.label.config(image="", text=f"Cannot open {name}: {error}")
            return
        self.label.config(image=photo, text="")
        self.label.image = photo

    def _move(self, step: int) -> None:
        target = self.index + step
        if 0 <= target < len(self.images):
            self.index = target
            self.furthest = max(self.furthest, target)
            self._show()

    def _mark(self) -> None:
        name = self.images[self.index]
        if name not in self.marked:
            self.marked.append(name)
            self.on_change()
            print(f"Appended: {name}")
            self._show()

    def _unmark(self) -> None:
        name = self.images[self.index]
        if name in self.marked:
            self.marked.remove(name)
            self.on_change()
            print(f"Removed: {name}")
            self._show()


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
def needs_project(method):
    """Raise a clear error if create()/restore() hasn't been called yet."""
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.state is None:
            raise RuntimeError("No project loaded: call create() or restore() first.")
        return method(self, *args, **kwargs)
    return wrapper


class Project:
    def __init__(self):
        self.root: Path | None = None
        self.state: State | None = None

    @property
    def name(self) -> str | None:
        return self.state.name if self.state else None

    # -- lifecycle ---------------------------------------------------------- #
    def create(self, name: str) -> None:
        self.root = Path(name).resolve()
        self.root.mkdir(parents=True, exist_ok=False)  # never overwrite an existing project
        for sub in (*KIND_DIRS.values(), GRAPHS_DIR):
            (self.root / sub).mkdir()
        self.state = State(name=name)
        self.save()
        print(f"Successfully created {name}!!")

    def restore(self, name: str) -> None:
        root = Path(name).resolve()
        config_path = root / CONFIG_NAME
        if not config_path.is_file():
            raise FileNotFoundError(f"No {CONFIG_NAME} found in {root}")
        self.root, self.state = root, State.load(config_path)
        print(f"Successfully restored {name}!!")

    @needs_project
    def save(self) -> None:
        _write_json_atomically(self.root / CONFIG_NAME, asdict(self.state))

    updateconfig = save  # old name

    # -- stage 1: download -------------------------------------------------- #
    @needs_project
    def download(self, kind: str, script_path: str, amount: int, workers: int = 4) -> None:
        """Download the first `amount` files of a curl script (`workers` run in parallel)."""
        if kind not in KIND_DIRS:
            raise ValueError(f"kind must be one of {sorted(KIND_DIRS)}, got {kind!r}")
        if self.state.kind not in (None, kind):
            raise ValueError(f"This project holds {self.state.kind!r} files; "
                             "use a separate project for a different type.")
        self.state.kind, self.state.script = kind, str(script_path)

        commands = read_commands(script_path, amount)
        done = set(self.state.downloaded)
        pending = [name for name in commands if name not in done]
        job = partial(_download, commands=commands, cwd=self.root / KIND_DIRS[kind])

        self._run_stage(job, pending, total=len(commands), workers=workers,
                        executor_cls=ThreadPoolExecutor,  # downloads wait on the network
                        done=self.state.downloaded, failed=self.state.download_failed)

    # -- stage 2: process --------------------------------------------------- #
    @needs_project
    def process(self, amount: int, workers: int = 1) -> None:
        """Plot the first `amount` downloaded files (raise `workers` to use more CPU cores)."""
        files = self.state.downloaded[:amount]
        done = set(self.state.processed)
        pending = [name for name in files if image_name(name) not in done]
        job = partial(_render, kind=self.state.kind,
                      source_dir=self.root / KIND_DIRS.get(self.state.kind, ""),
                      graphs_dir=self.root / GRAPHS_DIR)

        self._run_stage(job, pending, total=len(files), workers=workers,
                        executor_cls=ProcessPoolExecutor,  # plotting is CPU-bound
                        done=self.state.processed, failed=self.state.process_failed,
                        record=image_name)

    @needs_project
    def indprocess(self, filename: str) -> str:
        """Plot a single downloaded file and return the image name."""
        _render(filename, self.state.kind,
                self.root / KIND_DIRS[self.state.kind], self.root / GRAPHS_DIR)
        return image_name(filename)

    # -- stage 3: review ---------------------------------------------------- #
    @needs_project
    def finalise(self, amount: int) -> None:
        """Open the viewer on the not-yet-reviewed images among the first `amount`."""
        pending = self.state.processed[:amount][self.state.reviewed:]
        if not pending:
            print("Nothing left to review.")
            return
        reviewer = ImageReviewer(self.root / GRAPHS_DIR, pending, self.state.marked, self.save)
        self.state.reviewed += reviewer.run()
        self.save()

    # -- results ------------------------------------------------------------ #
    @needs_project
    def getresults(self) -> list[str]:
        """Unique TIC IDs of the marked images, in the order they were marked."""
        ids = (tic_id(name) for name in self.state.marked)
        return list(dict.fromkeys(tic for tic in ids if tic))

    def saveresults(self, name: str) -> None:
        Path(name).write_text(json.dumps(self.getresults()), encoding="utf-8")

    # -- shared stage runner ------------------------------------------------ #
    def _run_stage(self, job, pending: list[str], *, total: int, workers: int, executor_cls,
                   done: list[str], failed: list[str],
                   record: Callable[[str], str] = lambda item: item) -> None:
        """Run `job` over `pending`, tracking successes/failures and saving periodically."""
        finished = total - len(pending)
        unsaved = 0
        try:
            for item, error in _run_jobs(job, pending, workers, executor_cls):
                finished += 1
                if error is not None:
                    print(f"{finished}/{total} FAILED: {item} ({error})")
                    if item not in failed:
                        failed.append(item)
                    continue
                done.append(record(item))
                if item in failed:
                    failed.remove(item)
                print(f"{finished}/{total}")
                unsaved += 1
                if unsaved >= SAVE_EVERY:
                    self.save()
                    unsaved = 0
        finally:
            self.save()  # also runs on Ctrl-C, so nothing finished is lost
