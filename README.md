# TESSify
A Python package for helping with detecting exoplanet transit dips in TESS light curves.

---

## 🚀 Features

- Load and clean `.fits` light curve or target pixel files
- Visualize light curves and transit candidates
- Designed for students, researchers, and citizen scientists
- Easily download and process raw data in bulk
- Easily shortlist potential exoplanet candidates

---

## 📦 Installation

```bash
pip install exoplanet_finder
```

---

## 🛠️ Usage

Importing TESSify

```python
from TESSify import Project
```

Creating a Project

```python
a = Project()
a.create("YOUR_PROJECT_NAME")
```

Restoring a Project

```python
a.restore("YOUR_PROJECT_NAME")
```

Downloading `.fits` files in bulk

```python
#You can get bulk downloading scripts from the official TESS archive at `https://archive.stsci.edu/tess/bulk_downloads/bulk_downloads_ffi-tp-lc-dv.html`
#You can download either Target Pixel (tp) or Light Curve (lc) files. After download, copy the path of the file.

from TESSify import Project

a = Project()

a.create("Project1")

a.download("lc", r"C:\Users\Expert\Downloads\tesscurl_sector_89_lc.sh", 100)
#Enter "lc" for lightcurve and "tp" for targetpixel
#Enter the path of the script
#Enter the amount of files to be downloaded

```

Processing the files in bulk

```python
#After Downloading the files, now its time to process them

from TESSify import Project

a = Project()

a.restore("Project1")

a.process(100)
#Enter the amount of files to process (It cannot be greater than the ones you have downloaded)
```
