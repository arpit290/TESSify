# TESSify
A Python package for helping with detecting exoplanet transit dips in TESS light curves.

---

## 🚀 Features

- Load and clean `.fits` light curve or target pixel files
- Visualize flattened light curves and shortlist transit candidates
- Download and process data in bulk, with parallel downloads and optional multi-core processing
- Resumable at every stage: stop at any time (even with Ctrl-C) and pick up where you left off
- Failed downloads and plots are recorded and retried on the next run instead of crashing the batch
- Designed for students, researchers, and citizen scientists

---

## 📦 Installation

Requires **Python 3.9 or newer**.

```bash
pip install TESSify
```

Or install the dependencies and run from source:

```bash
pip install -r requirements.txt
```

Two things pip can't install for you:

- **`curl`** must be available on your PATH. It is used to run the bulk download scripts. It ships with Windows 10+, macOS, and most Linux distributions.
- **`tkinter`** is needed for the image viewer. It is bundled with the Windows and macOS Python installers. On Debian/Ubuntu, run `sudo apt install python3-tk`. Without it, downloading and processing still work; only `finalise()` needs it.

---

## 🛠️ Usage

The typical workflow is: **create → download → process → finalise → get results**.

### Importing TESSify

```python
from TESSify import Project
```

### Creating a Project

```python
a = Project()
a.create("YOUR_PROJECT_NAME")
```

This makes a new folder with that name. `create` refuses to overwrite a project that already exists.

### Restoring a Project

```python
a = Project()
a.restore("YOUR_PROJECT_NAME")
```

### Downloading `.fits` files in bulk

Get a bulk download script from the official [TESS archive](https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_ffi-tp-lc-dv.html). You can download either Target Pixel (`tp`) or Light Curve (`lc`) files. After downloading the script, copy its path.

```python
from TESSify import Project

a = Project()
a.create("Project1")

a.download("lc", r"C:\Users\Expert\Downloads\tesscurl_sector_89_lc.sh", 100)
# 1st argument: "lc" for light curves, "tp" for target pixel files
# 2nd argument: path to the download script
# 3rd argument: how many files to download
```

Optional: download several files at once (default is 4).

```python
a.download("lc", "tesscurl_sector_89_lc.sh", 100, workers=8)
```

Notes:

- Running `download` again continues where it stopped: files that are already downloaded are skipped, and files that failed are retried.
- To fetch more files from the same script, call `download` again with a larger amount.
- A project holds one file type. To work with both `lc` and `tp` files, create two projects.

### Processing the files in bulk

Processing cleans each file (removes NaNs, flattens the curve) and saves a plot of it in the project's `Graphs` folder.

```python
from TESSify import Project

a = Project()
a.restore("Project1")

a.process(100)
# The amount can't usefully exceed the number of files you have downloaded
```

Optional: use several CPU cores (default is 1).

```python
a.process(100, workers=4)
```

> **Windows/macOS users:** when using `workers` greater than 1, put your script's top-level code under `if __name__ == "__main__":`. This is the standard rule for Python multiprocessing. Each worker also loads its own copy of the data, so keep `workers` modest if you are short on memory.

Files that can't be read (for example, a corrupt download) are skipped, reported in the console, and listed under `process_failed` in `config.json`. Re-running `process` retries them.

To process a single file and get the image name back:

```python
image = a.indprocess("tess2019198215352-s0014-0000000261136679-0150-s_lc.fits")
```

### Finalising the light curves and watching out for transit dips

After processing, you can flip through the graphs and shortlist any that show dips that could indicate an exoplanet.

```python
from TESSify import Project

a = Project()
a.restore("Project1")

a.finalise(100)
# Opens a window showing the processed graphs
```

Viewer controls:

| Key | Action |
|-----|--------|
| **Right arrow** | Next graph |
| **Left arrow** | Previous graph |
| **Down arrow** | Shortlist the current graph (if you see something of interest) |
| **Up arrow** | Remove the current graph from the shortlist |
| **Escape** (or closing the window) | Save and exit |

The title bar shows your position and whether the current graph is shortlisted. Your shortlist is saved as you go, and the next call to `finalise` starts after the last graph you reached.

### Getting the list of shortlisted entities

```python
from TESSify import Project

a = Project()
a.restore("Project1")

finalised_list = a.getresults()
# Returns unique TIC IDs in the order you shortlisted them

print(finalised_list)
# ['TIC 261136679', 'TIC 25155310']
```

### Saving the shortlisted entities as a JSON file

```python
from TESSify import Project

a = Project()
a.restore("Project1")

a.saveresults("shortlist.json")
# Any path works, e.g. r"C:\Users\Expert\Documents\shortlist.json" or "/home/expert/shortlist.json"
```

---

## 📁 Project layout

```
YOUR_PROJECT_NAME/
├── config.json       # progress for every stage (safe to leave alone)
├── LightCurves/      # downloaded .fits light curves (type "lc")
├── TargetPixels/     # downloaded .fits target pixel files (type "tp")
└── Graphs/           # plots made by process()
```

`config.json` is written safely, so a crash can't leave it half-written. Projects created with older versions of TESSify load automatically. Re-run `process` on them, because plot names created by older versions may not match.

---

## ❓ Troubleshooting

- **`FileExistsError` on `create`:** a folder with that name already exists. Use `restore` instead, or pick a new name.
- **`FileNotFoundError` on `restore`:** the folder doesn't contain a `config.json`. Check the name and your working directory, since projects are found relative to where you run Python.
- **Downloads show `FAILED`:** check your connection and that `curl` is installed, then re-run `download`. Failed files are retried automatically.
- **`The viewer needs tkinter and Pillow`:** install them as described under [Installation](#-installation).

---

## 🧠 Contributing
Contributions are welcome! Open an issue or submit a pull request at [github.com/arpit290/TESSify](https://github.com/arpit290/TESSify).

---

## 📜 License
This project is licensed under the MIT License. See `LICENSE` for details.

---

## 👤 Author
Arpit Bishnoi
[github.com/arpit290](https://github.com/arpit290) · bishnoiarpit29@gmail.com
