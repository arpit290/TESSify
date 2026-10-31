"""TESS light-curve / target-pixel project manager (needs Python 3.10+).

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
import shutil
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
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
DEFAULT_WINDOW_LENGTH = 101      # lightkurve's own default for flatten()
DEFAULT_APERTURE_MASK = "all"    # target pixel files only: which pixels form the light curve
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


def _write_json_atomically(path: Path, data: object) -> None:
    """Write via a temp file so a crash never leaves a half-written config."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _check_window_length(window_length: int) -> None:
    """flatten() needs an odd integer window of at least 3 points."""
    if not isinstance(window_length, int) or window_length < 3 or window_length % 2 == 0:
        raise ValueError(f"window_length must be an odd integer >= 3, got {window_length!r}")


# --------------------------------------------------------------------------- #
# Workers (module-level so they can be sent to other processes)
# --------------------------------------------------------------------------- #
def _download(filename: str, commands: dict[str, list[str]], cwd: Path) -> None:
    result = subprocess.run(commands[filename], cwd=cwd)
    if result.returncode != 0 or not (cwd / filename).is_file():
        raise RuntimeError(f"download exited with code {result.returncode}")


def _render(filename: str, kind: str, source_dir: Path, graphs_dir: Path,
            window_length: int = DEFAULT_WINDOW_LENGTH,
            aperture_mask: str = DEFAULT_APERTURE_MASK) -> None:
    """Clean one FITS file and save its plot.

    window_length: smoothing window (in data points) used by flatten().
    aperture_mask: pixels summed into a light curve; only used for target pixel files.
    """
    import matplotlib
    matplotlib.use("Agg")  # no GUI windows, no clash with the Tk viewer
    import matplotlib.pyplot as plt
    import lightkurve as lk

    data = lk.read(source_dir / filename)
    if kind == "tp":
        data = data.to_lightcurve(aperture_mask=aperture_mask)
    lightcurve = data.remove_nans().flatten(window_length=window_length)

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
    """Run jobs while bounding queued futures to avoid large per-file overhead."""
    if workers <= 1:  # inline: simpler tracebacks, no pool start-up cost
        for item in items:
            try:
                job(item)
                yield item, None
            except Exception as error:
                yield item, error
        return

    pool = executor_cls(max_workers=workers)
    iterator = iter(items)
    futures: dict[object, str] = {}

    def submit_one() -> bool:
        try:
            item = next(iterator)
        except StopIteration:
            return False
        futures[pool.submit(job, item)] = item
        return True

    try:
        # Keep at most 2 * workers jobs queued. This avoids allocating thousands
        # of Future objects when a user processes a large archive.
        for _ in range(max(1, workers * 2)):
            if not submit_one():
                break

        while futures:
            completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in completed:
                item = futures.pop(future)
                try:
                    future.result()
                    error = None
                except Exception as caught:
                    error = caught
                yield item, error
                submit_one()
    finally:  # on Ctrl-C: let running jobs finish, drop queued jobs
        for future in futures:
            future.cancel()
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
    reviewed: int = 0                       # count of currently existing reviewed images
    marked: list[str] = field(default_factory=list)           # images flagged in the viewer
    review_index: int = 0                    # current position in the existing-image list
    reviewed_images: list[str] = field(default_factory=list)  # durable review identity
    review_current: str | None = None        # filename at review_index, if available

    @classmethod
    def load(cls, path: Path) -> State:
        data = json.loads(path.read_text(encoding="utf-8"))
        if "Download_Progress" in data:  # config written by the older versions
            data = cls._from_legacy(data)

        # Older configs only stored a high-water count. Migrate that count to
        # filenames once, preserving the old sequential-review interpretation.
        if "reviewed_images" not in data:
            processed = data.get("processed", [])
            count = data.get("reviewed", 0)
            if not isinstance(count, int) or count < 0:
                count = 0
            data["reviewed_images"] = processed[:count] if isinstance(processed, list) else []

        known = {f.name for f in fields(cls)}
        state = cls(**{key: value for key, value in data.items() if key in known})
        if not isinstance(state.reviewed_images, list):
            state.reviewed_images = []
        if not isinstance(state.marked, list):
            state.marked = []
        state.reviewed_images = list(dict.fromkeys(
            name for name in state.reviewed_images if isinstance(name, str)
        ))
        state.marked = list(dict.fromkeys(
            name for name in state.marked if isinstance(name, str)
        ))
        if (not isinstance(state.review_index, int) or isinstance(state.review_index, bool)
                or state.review_index < 0):
            state.review_index = 0
        if state.review_current is not None and not isinstance(state.review_current, str):
            state.review_current = None

        # Map the saved cursor against the files that actually exist. This is
        # important when a graph was removed between sessions: raw processed-list
        # indices no longer match the reviewer's filtered image list.
        graphs_dir = path.parent / GRAPHS_DIR
        processed = state.processed if isinstance(state.processed, list) else []
        existing = [
            name for name in processed
            if isinstance(name, str) and Path(name).name == name
            and (graphs_dir / name).is_file()
        ]
        valid = set(existing)
        state.reviewed_images = [name for name in state.reviewed_images if name in valid]
        state.marked = [name for name in state.marked if name in valid]
        state.reviewed = len(state.reviewed_images)
        if state.review_current in valid:
            state.review_index = existing.index(state.review_current)
        else:
            # For configs created before review_current existed, infer the old
            # selected filename from the unfiltered processed list before
            # translating it into the filtered list's new index.
            candidate = None
            if state.review_current is None and state.review_index < len(processed):
                candidate = processed[state.review_index]
            if isinstance(candidate, str) and candidate in valid:
                state.review_current = candidate
                state.review_index = existing.index(candidate)
            elif existing:
                state.review_index = min(state.review_index, len(existing) - 1)
                state.review_current = existing[state.review_index]
            else:
                state.review_index = 0
                state.review_current = None
        return state

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
            "review_index": 0,
        }


# --------------------------------------------------------------------------- #
# Image viewer
# --------------------------------------------------------------------------- #
class ImageReviewer:
    """Tk window for stepping through plots.

    Right/Left: navigate    Down: mark    Up: unmark    Esc or close: finish
    """

    def __init__(self, graphs_dir: Path, images: list[str],
                 marked: list[str], on_change: Callable[[], None],
                 on_review: Callable[[str], None] | None = None):
        if tk is None:
            raise RuntimeError("The viewer needs tkinter and Pillow (pip install pillow).")
        self.graphs_dir = graphs_dir
        self.images = images
        self.marked = marked          # shared with the project state, edited in place
        self.on_change = on_change
        self.on_review = on_review
        self._visited: set[str] = set()
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
        if name not in self._visited:
            self._visited.add(name)
            if self.on_review is not None:
                self.on_review(name)

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
        self.state = State(name=self.root.name)
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
    def download(self, kind: str, script_path: str, amount: int, workers: int = 4,
                 progress_callback: Callable[[int, int, str, Exception | None], None] | None = None) -> None:
        """Download the first `amount` files of a curl script (`workers` run in parallel).

        `progress_callback`, when supplied, receives (finished, total, filename, error)
        in the calling thread. It is optional and leaves the existing API compatible.
        """
        if kind not in KIND_DIRS:
            raise ValueError(f"kind must be one of {sorted(KIND_DIRS)}, got {kind!r}")
        if not isinstance(amount, int) or amount < 1:
            raise ValueError("amount must be a positive integer")
        if not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        script = Path(script_path).expanduser().resolve()
        if not script.is_file():
            raise FileNotFoundError(f"Download script not found: {script}")
        if shutil.which("curl") is None:
            raise FileNotFoundError("curl was not found on PATH; TESSify's bulk downloader requires curl.")
        if self.state.kind not in (None, kind):
            raise ValueError(f"This project holds {self.state.kind!r} files; "
                             "use a separate project for a different type.")
        self.state.kind, self.state.script = kind, str(script)

        commands = read_commands(script, amount)
        download_dir = self.root / KIND_DIRS[kind]
        # A stale config entry must not make a deleted FITS file look complete.
        existing_downloads = {
            name for name in self.state.downloaded
            if Path(name).name == name and (download_dir / name).is_file()
        }
        self.state.downloaded = list(dict.fromkeys(
            name for name in self.state.downloaded if name in existing_downloads
        ))
        done = set(existing_downloads)
        pending = [name for name in commands if name not in done]
        job = partial(_download, commands=commands, cwd=download_dir)

        self._run_stage(job, pending, total=len(commands), workers=workers,
                        executor_cls=ThreadPoolExecutor,  # downloads wait on the network
                        done=self.state.downloaded, failed=self.state.download_failed,
                        progress_callback=progress_callback)

    # -- stage 2: process --------------------------------------------------- #
    @needs_project
    def process(self, amount: int, workers: int = 1,
                window_length: int = DEFAULT_WINDOW_LENGTH,
                aperture_mask: str = DEFAULT_APERTURE_MASK,
                progress_callback: Callable[[int, int, str, Exception | None], None] | None = None) -> None:
        """Plot the first `amount` downloaded files.

        workers:       CPU cores to use (1 = run in this process).
        window_length: odd number of data points in flatten()'s smoothing window. Use a window
                       several times longer than the transits you are looking for.
        aperture_mask: how target pixel ("tp") files become light curves: "all", "pipeline"
                       or "threshold". Ignored for light curve ("lc") files.

        Files that already have a plot are skipped, so new settings only affect new files.
        """
        _check_window_length(window_length)
        if not isinstance(amount, int) or amount < 1:
            raise ValueError("amount must be a positive integer")
        if not isinstance(workers, int) or workers < 1:
            raise ValueError("workers must be a positive integer")
        if self.state.kind not in KIND_DIRS:
            raise RuntimeError("No TESS data type is set for this project. Download data first.")
        files = self.state.downloaded[:amount]
        graphs_dir = self.root / GRAPHS_DIR
        # If a generated plot was deleted, allow the corresponding FITS input to
        # be processed again instead of trusting stale config state.
        existing_processed = {
            name for name in self.state.processed
            if isinstance(name, str) and Path(name).name == name and (graphs_dir / name).is_file()
        }
        # A missing graph is going to be regenerated. Treat it as a new image:
        # remove its old review/shortlist state so it cannot be silently skipped.
        self.state.processed = list(dict.fromkeys(
            name for name in self.state.processed if name in existing_processed
        ))
        self._reconcile_review_state(self.state.processed)
        done = set(existing_processed)
        pending = [name for name in files if image_name(name) not in done]
        job = partial(_render, kind=self.state.kind,
                      source_dir=self.root / KIND_DIRS.get(self.state.kind, ""),
                      graphs_dir=self.root / GRAPHS_DIR,
                      window_length=window_length, aperture_mask=aperture_mask)

        self._run_stage(job, pending, total=len(files), workers=workers,
                        executor_cls=ProcessPoolExecutor,  # plotting is CPU-bound
                        done=self.state.processed, failed=self.state.process_failed,
                        record=image_name, progress_callback=progress_callback)

    @needs_project
    def indprocess(self, filename: str,
                   window_length: int = DEFAULT_WINDOW_LENGTH,
                   aperture_mask: str = DEFAULT_APERTURE_MASK) -> str:
        """Plot a single downloaded file (same settings as process()) and return the image name."""
        _check_window_length(window_length)
        if self.state.kind not in KIND_DIRS:
            raise RuntimeError("No TESS data type is set for this project. Download data first.")
        output_name = image_name(filename)
        was_processed = output_name in self.state.processed
        _render(filename, self.state.kind,
                self.root / KIND_DIRS[self.state.kind], self.root / GRAPHS_DIR,
                window_length=window_length, aperture_mask=aperture_mask)
        if was_processed:
            # indprocess() overwrites the plot too, so prior review and shortlist
            # decisions no longer describe the newly generated image.
            self.state.reviewed_images = [
                name for name in self.state.reviewed_images if name != output_name
            ]
            self.state.marked = [
                name for name in self.state.marked if name != output_name
            ]
            if self.state.review_current == output_name:
                self.state.review_current = None
                self.state.review_index = 0
            self._reconcile_review_state(self.get_review_images())
            self.save()
        return output_name

    # -- review state API --------------------------------------------------- #
    @needs_project
    def get_review_images(self, amount: int | None = None) -> list[str]:
        """Return existing processed images, in package order, without duplicates."""
        if amount is not None and (not isinstance(amount, int) or amount < 0):
            raise ValueError("amount must be a non-negative integer or None")
        names = self.state.processed if amount is None else self.state.processed[:amount]
        graphs = self.root / GRAPHS_DIR
        result: list[str] = []
        seen: set[str] = set()
        for name in names:
            if (isinstance(name, str) and Path(name).name == name
                    and name not in seen and (graphs / name).is_file()):
                result.append(name)
                seen.add(name)
        return result

    def _reconcile_review_state(self, valid_images: Iterable[str]) -> None:
        """Drop review references to images that are no longer in the project."""
        ordered = list(valid_images)
        valid = set(ordered)
        self.state.reviewed_images = list(dict.fromkeys(
            name for name in self.state.reviewed_images if name in valid
        ))
        self.state.marked = list(dict.fromkeys(
            name for name in self.state.marked if name in valid
        ))
        self.state.reviewed = len(self.state.reviewed_images)
        if self.state.review_current in valid:
            self.state.review_index = ordered.index(self.state.review_current)
        else:
            self.state.review_current = None
            self.state.review_index = 0

    def _record_reviewed(self, filename: str) -> None:
        """Persist one image as reviewed when the Tk viewer successfully displays it."""
        images = self.get_review_images()
        if filename not in images:
            return
        self._reconcile_review_state(images)
        if filename not in self.state.reviewed_images:
            self.state.reviewed_images.append(filename)
        self.state.reviewed = len(self.state.reviewed_images)
        self.state.review_current = filename
        self.state.review_index = images.index(filename)
        self.save()

    @needs_project
    def set_review_state(self, marked: Iterable[str] | None = None,
                         review_index: int | None = None) -> None:
        """Persist Qt review state using filenames as durable image identities."""
        old_marked = list(self.state.marked)
        old_reviewed_images = list(self.state.reviewed_images)
        old_index = self.state.review_index
        old_current = self.state.review_current
        old_reviewed = self.state.reviewed
        try:
            images = self.get_review_images()
            self._reconcile_review_state(images)
            valid = set(images)
            if marked is not None:
                self.state.marked = list(dict.fromkeys(
                    name for name in marked if isinstance(name, str) and name in valid
                ))
            else:
                self.state.marked = [name for name in self.state.marked if name in valid]

            self.state.reviewed_images = [
                name for name in self.state.reviewed_images if name in valid
            ]
            if review_index is not None:
                if not isinstance(review_index, int) or isinstance(review_index, bool) or review_index < 0:
                    raise ValueError("review_index must be a non-negative integer")
                if images:
                    index = min(review_index, len(images) - 1)
                    # Preserve the old high-water behavior: reaching image N means
                    # every image before N has necessarily been traversed as well.
                    reviewed_set = set(self.state.reviewed_images)
                    self.state.reviewed_images.extend(
                        name for name in images[:index + 1] if name not in reviewed_set
                    )
                    self.state.review_current = images[index]
                    self.state.review_index = index
                else:
                    self.state.review_current = None
                    self.state.review_index = 0

            self.state.reviewed = len(set(self.state.reviewed_images) & valid)
            self.save()
        except Exception:
            self.state.marked = old_marked
            self.state.reviewed_images = old_reviewed_images
            self.state.review_index = old_index
            self.state.review_current = old_current
            self.state.reviewed = old_reviewed
            raise

    # -- stage 3: review ---------------------------------------------------- #
    @needs_project
    def finalise(self, amount: int) -> None:
        """Review unreviewed, existing graphs among the first `amount` processed images."""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("amount must be a non-negative integer")
        all_images = self.get_review_images()
        self._reconcile_review_state(all_images)
        self.save()
        review_scope = self.get_review_images(amount)
        reviewed = set(self.state.reviewed_images)
        pending = [name for name in review_scope if name not in reviewed]
        if not pending:
            print("Nothing left to review.")
            return
        reviewer = ImageReviewer(
            self.root / GRAPHS_DIR, pending, self.state.marked, self.save,
            on_review=self._record_reviewed,
        )
        reviewer.run()
        self.save()

    # -- results ------------------------------------------------------------ #
    @needs_project
    def getresults(self) -> list[str]:
        """Unique TIC IDs from marked images whose graph files still exist."""
        valid = set(self.get_review_images())
        ids = (tic_id(name) for name in self.state.marked if name in valid)
        return list(dict.fromkeys(tic for tic in ids if tic))

    def saveresults(self, name: str) -> None:
        """Export unique shortlisted TIC IDs as the package's original JSON list format."""
        output = Path(name).expanduser().resolve()
        tmp = output.with_name(output.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.getresults(), indent=2), encoding="utf-8")
            os.replace(tmp, output)
        finally:
            if tmp.exists():
                tmp.unlink()

    # -- shared stage runner ------------------------------------------------ #
    def _run_stage(self, job, pending: list[str], *, total: int, workers: int, executor_cls,
                   done: list[str], failed: list[str],
                   record: Callable[[str], str] = lambda item: item,
                   progress_callback: Callable[[int, int, str, Exception | None], None] | None = None) -> None:
        """Run `job` over `pending`, tracking successes/failures and saving periodically."""
        finished = total - len(pending)
        unsaved = 0
        done_lookup = set(done)
        failed_lookup = set(failed)

        def notify(item: str, error: Exception | None) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(finished, total, item, error)
                except Exception as callback_error:
                    # UI/log callbacks must never corrupt the scientific pipeline.
                    print(f"Progress callback failed: {callback_error}")

        notify("", None)  # publish real initial state, including a fully resumed stage
        try:
            for item, error in _run_jobs(job, pending, workers, executor_cls):
                finished += 1
                if error is not None:
                    print(f"{finished}/{total} FAILED: {item} ({error})")
                    if item not in failed_lookup:
                        failed.append(item)
                        failed_lookup.add(item)
                    notify(item, error)
                    continue
                recorded = record(item)
                if recorded not in done_lookup:
                    done.append(recorded)
                    done_lookup.add(recorded)
                if item in failed_lookup:
                    failed.remove(item)
                    failed_lookup.remove(item)
                print(f"{finished}/{total}")
                unsaved += 1
                if unsaved >= SAVE_EVERY:
                    self.save()
                    unsaved = 0
                notify(item, None)
        finally:
            self.save()  # also runs on Ctrl-C, so nothing finished is lost
