"""Dev-PC release tool:  python MakeRelease.py
Checks APP_VER (zaia_pdf.py) == AppVersion (.iss), writes update.json (SHA-256 of every
updatable file, version, setup_rev) and builds the full ZIP.
Bump setup_rev.txt when anything outside UPDATABLE changes (exe, iss, new pip packages)."""
import re, json, hashlib, zipfile, sys
from pathlib import Path

D = Path(__file__).resolve().parent
UPDATABLE = ["zaia_pdf.py", "zaia.ico", "logo.png", "ZAIA_PDF.bat",
             "tessdata/tha.traineddata", "fonts/Sarabun-Regular.ttf", "fonts/OFL.txt"]  # OCR language data + invisible-text font
EXTRA = ["ZAIA_PDF.exe", "Launcher.cs", "Build_All.bat", "build_all.py", "ZAIA_PDF.iss", "MakeRelease.py", "setup_rev.txt", "update.json"]


def die(msg):
    sys.exit("ERROR: " + msg)


app_ver = re.search(r'^APP_VER\s*=\s*"([\d.]+)"', (D / "zaia_pdf.py").read_text(encoding="utf-8"), re.M)
iss_ver = re.search(r'^#define AppVersion\s+"([\d.]+)"', (D / "ZAIA_PDF.iss").read_text(encoding="utf-8"), re.M)
if not app_ver or not iss_ver:
    die("cannot read version from zaia_pdf.py / ZAIA_PDF.iss")
if app_ver.group(1) != iss_ver.group(1):
    die("APP_VER %s != AppVersion %s" % (app_ver.group(1), iss_ver.group(1)))
ver = app_ver.group(1)
try:
    rev = int((D / "setup_rev.txt").read_text().strip())
except Exception:
    die("setup_rev.txt must hold one integer")
for f in UPDATABLE:
    if not (D / f).exists():
        die("missing " + f)

files = {f: hashlib.sha256((D / f).read_bytes()).hexdigest() for f in UPDATABLE}
(D / "update.json").write_text(json.dumps({"version": ver, "setup_rev": rev, "files": files}, indent=2) + "\n", encoding="utf-8")

if "--no-zip" in sys.argv:
    print("wrote update.json (version %s)" % ver)
    sys.exit(0)
name = "ZAIA_PDF_%s%s.zip" % ("Beta_" if ver.split(".")[0] == "0" else "", ver)
with zipfile.ZipFile(D / name, "w", zipfile.ZIP_DEFLATED) as z:
    for w in sorted((D / "wheels").glob("*.whl")):
        z.write(w, "wheels/" + w.name)
    for f in UPDATABLE + EXTRA:
        if (D / f).exists():
            z.write(D / f, f)
        else:
            print("skip (not built yet):", f)
print("version", ver, "setup_rev", rev)
for f, h in files.items():
    print(" ", h[:12], f)
print("wrote update.json +", name)
