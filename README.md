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
pip install TESSify
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

Finalising the LightCurves and watching out for Transit Dips

```python
#After you have processed the files, you can see their graphs to see if there are any dips that would indicate the esistence of an exoplanet

from TESSify import Project

a = Project()

a.restore("Project1")

a.finalise(100)
#Enter the amount of files to finalise (It cannot be greater than the ones you have processed)
#This would open a window which you can navigate by:
# "Right Keyboard Button" - Next Slide
# "Left Keyboard Button" - Previous Slide
# "Down Keyboard Button" - Shortlist Slide (If you see something of interest)
# "Escape Button" - Exit Program
```

Getting the List of the Shortlisted Entities

```python
#After Shortlisting several potential candidates, you might want them in a list format

from TESSify import Project

a = Project()

a.restore("Project1")

finalised_list = a.getresults()
# This returns a list in the TIC + ID format

print(finalised_list)
```

Saving the Shortlisted Entities as a JSON file

```python
#You want to save the shortlisted entities in a JSON
from TESSify import Project

a = Project()

a.restore("Project1")

filepath = "Your file path"
#Enter the path to your file along with your filename and extension EX:- C:\Users\Expert\Documents\Programming\address.json

a.saveresults(filepath)
```

---
## 🧠 Contributing
Contributions are welcome! Open an issue or submit a pull request at `https://github.com/arpit290/TESSify`

---
## 📜 License
This project is licensed under the MIT License. See `LICENSE` for details.

---
## 👤 Author
Arpit Bishnoi
`https://github.com/arpit290` · `bishnoiarpit29@gmail.com`
