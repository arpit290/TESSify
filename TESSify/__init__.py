"""TESS light-curve / target-pixel project manager.

Workflow:
    p = Project(); p.create("my_project")        # or p.restore("my_project")
    p.download("lc", "bulk_download.sh", 100)    # fetch files listed in a MAST curl script
    p.process(100)                               # render a flattened plot for each file
    p.finalise(100)                              # browse plots, press Down to mark candidates
    p.saveresults("candidates.json")             # write the marked TIC IDs

Every step is resumable: progress is stored in <project>/config.json.
"""
from __future__ import annotations

import gc
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

DOWNLOAD_DIRS = {"lc": "LightCurves", "tp": "TargetPixels"}
CONFIG_NAME = "config.json"
IMAGE_EXT = ".jpeg"
_TIC_RE = re.compile(r"-s\d{4}-(\d{16})-")


def _default_config(name: str) -> dict:
    return {
        "Name": name,
        "Download_Progress": {"Type": None, "Script": None, "ToDownload": None, "List": [], "Failed": []},
        "Processing_Progress": {"ToProcess": None, "List": [], "Failed": []},
        "Finalising_Progress": {"ToFinalise": None, "Finalised": 0, "List": []},
    }


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON via a temp file so a crash can never leave a half-written config."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _tic_from_filename(name: str) -> str | None:
    """'tess...-s0014-0000000261136679-0150-s_lc' -> 'TIC 261136679'."""
    match = _TIC_RE.search(name)
    if match is None:
        parts = name.split("-")
        digits = parts[2] if len(parts) > 2 else ""
        if not digits.isdigit():
            return None
    else:
        digits = match.group(1)
    return "TIC " + (digits.lstrip("0") or "0")


def _output_filename(tokens: list[str]) -> str:
    """Extract the output file name from a curl command (the argument of -o / --output)."""
    for flag in ("-o", "--output"):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens):
                return tokens[idx + 1]
    raise ValueError(f"Could not find an output file (-o) in command: {' '.join(tokens)}")


class Project:
    def __init__(self):
        self.name: str | None = None
        self.root: Path | None = None
        self.config: dict | None = None

    # ------------------------------------------------------------------ setup
    def create(self, name: str) -> None:
        self.root = Path(name).resolve()
        self.root.mkdir(parents=True, exist_ok=False)  # refuse to clobber an existing project
        for sub in (*DOWNLOAD_DIRS.values(), "Graphs"):
            (self.root / sub).mkdir()
        self.name = name
        self.config = _default_config(name)
        self._save()
        print(f"Successfully created {name}!!")

    def restore(self, name: str) -> None:
        root = Path(name).resolve()
        config_path = root / CONFIG_NAME
        if not config_path.is_file():
            raise FileNotFoundError(f"No {CONFIG_NAME} found in {root}")
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        # Merge over defaults so configs from older versions still work.
        config = _default_config(name)
        for section, values in loaded.items():
            if isinstance(values, dict) and isinstance(config.get(section), dict):
                config[section].update(values)
            else:
                config[section] = values
        self.root, self.name, self.config = root, name, config
        print(f"Successfully restored {name}!!")

    def _save(self) -> None:
        self._require_project()
        _atomic_write_json(self.root / CONFIG_NAME, self.config)

    # Kept for backwards compatibility with the old public method name.
    updateconfig = _save

    def _require_project(self) -> None:
        if self.root is None or self.config is None:
            raise RuntimeError("No project loaded: call create() or restore() first.")

    # --------------------------------------------------------------- download
    def download(self, kind: str, script_path: str, amount: int) -> None:
        """Run the first `amount` download commands of a curl script, skipping finished ones."""
        self._require_project()
        if kind not in DOWNLOAD_DIRS:
            raise ValueError(f"kind must be one of {sorted(DOWNLOAD_DIRS)}, got {kind!r}")
        progress = self.config["Download_Progress"]
        if progress["Type"] not in (None, kind):
            raise ValueError(f"This project already downloads {progress['Type']!r} files; "
                             "use a separate project for a different type.")

        with open(script_path, "r", encoding="utf-8") as script:
            commands = [line.strip() for line in script
                        if line.strip() and not line.lstrip().startswith("#")][:amount]

        progress.update(Type=kind, Script=str(script_path), ToDownload=len(commands))
        self._save()

        target = self.root / DOWNLOAD_DIRS[kind]
        done = set(progress["List"])
        try:
            for number, command in enumerate(commands, start=1):
                tokens = shlex.split(command)
                filename = Path(_output_filename(tokens)).name
                if filename in done:
                    continue
                result = subprocess.run(tokens, cwd=target)
                if result.returncode != 0 or not (target / filename).is_file():
                    print(f"{number}/{len(commands)} FAILED: {filename}")
                    if filename not in progress["Failed"]:
                        progress["Failed"].append(filename)
                    continue
                progress["List"].append(filename)
                done.add(filename)
                if filename in progress["Failed"]:
                    progress["Failed"].remove(filename)
                self._save()  # persist after every file so an interruption loses nothing
                print(f"{number}/{len(commands)}")
        finally:
            self._save()

    # ---------------------------------------------------------------- process
    def process(self, amount: int) -> None:
        """Render a plot for the first `amount` downloaded files, skipping finished ones."""
        self._require_project()
        progress = self.config["Processing_Progress"]
        files = self.config["Download_Progress"]["List"][:amount]
        progress["ToProcess"] = len(files)
        done = set(progress["List"])
        try:
            for number, filename in enumerate(files, start=1):
                image = Path(filename).stem + IMAGE_EXT
                if image in done:
                    continue
                try:
                    self.indprocess(filename)
                except Exception as exc:  # one corrupt file shouldn't kill the whole batch
                    print(f"{number}/{len(files)} FAILED: {filename} ({exc})")
                    if filename not in progress["Failed"]:
                        progress["Failed"].append(filename)
                    continue
                progress["List"].append(image)
                done.add(image)
                self._save()
                print(f"{number}/{len(files)}")
        finally:
            self._save()

    def indprocess(self, filename: str) -> str:
        """Clean one file, plot it and save the image. Returns the image file name."""
        self._require_project()
        import matplotlib
        matplotlib.use("Agg")  # no GUI windows, and no clash with the Tk viewer
        import matplotlib.pyplot as plt
        import lightkurve as lk

        kind = self.config["Download_Progress"]["Type"]
        if kind not in DOWNLOAD_DIRS:
            raise ValueError("Nothing has been downloaded yet.")
        source = self.root / DOWNLOAD_DIRS[kind] / filename

        data = lk.read(source)
        if kind == "tp":
            data = data.to_lightcurve(aperture_mask="all")
        lc = data.remove_nans().flatten()

        image = Path(filename).stem + IMAGE_EXT
        fig, ax = plt.subplots()
        try:
            lc.plot(ax=ax)
            fig.savefig(self.root / "Graphs" / image, bbox_inches="tight")
        finally:
            plt.close(fig)
            del data, lc
            gc.collect()
        return image

    # --------------------------------------------------------------- finalise
    def finalise(self, amount: int) -> None:
        """Open the viewer on the not-yet-reviewed images among the first `amount`."""
        self._require_project()
        progress = self.config["Finalising_Progress"]
        images = self.config["Processing_Progress"]["List"][:amount]
        progress["ToFinalise"] = len(images)
        self._save()
        self.indfinalise(images[progress["Finalised"]:], start=progress["Finalised"])

    def indfinalise(self, image_paths: list[str], start: int = 0) -> None:
        """Image viewer. Right/Left: navigate. Down: mark. Up: unmark. Esc/close: save and quit."""
        self._require_project()
        if not image_paths:
            print("Nothing left to review.")
            return

        import tkinter as tk
        from PIL import Image, ImageTk

        progress = self.config["Finalising_Progress"]
        graphs = self.root / "Graphs"
        state = {"index": 0, "furthest": 0}

        root = tk.Tk()
        max_size = (int(root.winfo_screenwidth() * 0.9), int(root.winfo_screenheight() * 0.85))
        label = tk.Label(root)
        label.pack()

        def show() -> None:
            name = image_paths[state["index"]]
            marked = "  [MARKED]" if name in progress["List"] else ""
            root.title(f"{state['index'] + 1}/{len(image_paths)}  {name}{marked}")
            try:
                with Image.open(graphs / name) as img:
                    img.thumbnail(max_size)
                    photo = ImageTk.PhotoImage(img)
                label.config(image=photo, text="")
                label.image = photo  # keep a reference or Tk garbage-collects it
            except OSError as exc:
                label.config(image="", text=f"Cannot open {name}: {exc}")

        def finish() -> None:
            progress["Finalised"] = start + state["furthest"] + 1
            self._save()
            root.destroy()

        def on_key(event) -> None:
            if event.keysym == "Right" and state["index"] < len(image_paths) - 1:
                state["index"] += 1
                state["furthest"] = max(state["furthest"], state["index"])
            elif event.keysym == "Left" and state["index"] > 0:
                state["index"] -= 1
            elif event.keysym == "Down":
                name = image_paths[state["index"]]
                if name not in progress["List"]:
                    progress["List"].append(name)
                    self._save()
                    print(f"Appended: {name}")
            elif event.keysym == "Up":
                name = image_paths[state["index"]]
                if name in progress["List"]:
                    progress["List"].remove(name)
                    self._save()
                    print(f"Removed: {name}")
            elif event.keysym == "Escape":
                finish()
                return
            else:
                return
            show()

        root.bind("<Key>", on_key)
        root.protocol("WM_DELETE_WINDOW", finish)  # closing with the X also saves progress
        show()
        root.mainloop()

    # ---------------------------------------------------------------- results
    def getresults(self) -> list[str]:
        """Unique TIC IDs of all marked images, in the order they were marked."""
        self._require_project()
        tics = (_tic_from_filename(name) for name in self.config["Finalising_Progress"]["List"])
        return list(dict.fromkeys(t for t in tics if t))

    def saveresults(self, name: str) -> None:
        Path(name).write_text(json.dumps(self.getresults()), encoding="utf-8")
