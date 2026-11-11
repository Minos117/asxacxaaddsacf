"""Build everything on a Windows PC (run via Build_All.bat):
  1. runtime\\  = embedded Python + PySide6 + PyMuPDF (from wheels\\, offline)
  2. ZAIA_PDF.exe (launcher)   3. update.json   4. Output\\ZAIA_PDF_Setup_x.y.z.exe (Inno Setup)
The Setup is self-contained: end users need no Python and no downloads.
Needs on THIS PC: 64-bit Python 3.10+, .NET Framework 4 (csc.exe), Inno Setup 6,
and internet once to fetch the embedded Python zip (or put python-*-embed-amd64.zip in deps\\)."""
import os, sys, shutil, struct, subprocess, zipfile, urllib.request
from pathlib import Path

D = Path(__file__).resolve().parent
PYV = "3.12.8"
EMBED_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (PYV, PYV)
RT = D / "runtime"
PKGS = ["PySide6-Essentials==6.12.0", "shiboken6==6.12.0", "PyMuPDF==1.26.7"]
CRT = ["msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"]
TRIM_DIRS = ["translations", "qml", "include", "glue", "typesystems", "scripts", "examples"]


def step(t):
    print("\n=== %s ===" % t, flush=True)


def run(cmd, **kw):
    if subprocess.run([str(c) for c in cmd], cwd=kw.pop("cwd", D), **kw).returncode:
        sys.exit("FAILED: " + " ".join(map(str, cmd)))


def embed_zip():
    local = sorted((D / "deps").glob("python-*-embed-amd64.zip"))
    if local:
        return local[-1]
    (D / "deps").mkdir(exist_ok=True)
    z = D / "deps" / Path(EMBED_URL).name
    print("downloading", EMBED_URL)
    try:
        urllib.request.urlretrieve(EMBED_URL, z)
    except Exception as e:
        z.unlink(missing_ok=True)
        sys.exit("cannot download embedded Python: %s\nDownload python-3.1x.x-embed-amd64.zip from python.org "
                 "and put it in %s, then run again." % (e, D / "deps"))
    return z


def trim(sp):
    for base in (sp / "PySide6", sp / "PySide6" / "Qt"):
        for n in TRIM_DIRS:
            shutil.rmtree(base / n, ignore_errors=True)
    shutil.rmtree(sp / "bin", ignore_errors=True)
    for exe in (sp / "PySide6").glob("*.exe"):  # designer, linguist, uic, rcc ... not needed at run time
        exe.unlink()
    for pyi in (sp / "PySide6").glob("*.pyi"):
        pyi.unlink()


def build_runtime():
    step("1/4 runtime (embedded Python + libs)")
    if struct.calcsize("P") != 8 or sys.version_info < (3, 10):
        sys.exit("need 64-bit Python 3.10+ to run this script")
    shutil.rmtree(RT, ignore_errors=True)
    RT.mkdir()
    with zipfile.ZipFile(embed_zip()) as f:
        bad = f.testzip()
        if bad:
            sys.exit("embedded Python zip is corrupt: " + bad)
        f.extractall(RT)
    pth = next(RT.glob("python*._pth"))
    pth.write_text("%s.zip\n.\nLib\\site-packages\n" % pth.stem, encoding="utf-8")
    sp = RT / "Lib" / "site-packages"
    sp.mkdir(parents=True)
    wh = D / "wheels"
    src = ["--no-index", "--find-links", wh] if wh.is_dir() and any(wh.glob("*.whl")) else []
    run([sys.executable, "-m", "pip", "install", "--target", sp, "--no-compile", "--only-binary=:all:", *src, *PKGS])
    trim(sp)
    sysd = Path(os.environ.get("WINDIR", "C:\\Windows")) / "System32"
    for n in CRT:  # app-local C++ runtime so the Setup works on PCs without VC++ redistributable
        if (sysd / n).exists():
            shutil.copy2(sysd / n, RT / n)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    run([RT / "python.exe", "-c", "import PySide6.QtWidgets,PySide6.QtPrintSupport,pymupdf;print('runtime OK')"], env=env)


def build_exe():
    step("2/4 ZAIA_PDF.exe")
    win = os.environ.get("WINDIR", "C:\\Windows")
    csc = next((p for p in (win + "\\Microsoft.NET\\Framework64\\v4.0.30319\\csc.exe",
                            win + "\\Microsoft.NET\\Framework\\v4.0.30319\\csc.exe") if Path(p).exists()), None)
    if not csc:
        sys.exit("csc.exe not found (.NET Framework 4)")
    run([csc, "/nologo", "/target:winexe", "/win32icon:zaia.ico", "/out:ZAIA_PDF.exe",
         "/r:System.Windows.Forms.dll", "Launcher.cs"])


def build_setup():
    step("3/4 update.json")
    run([sys.executable, "MakeRelease.py", "--no-zip"])
    step("4/4 Setup (Inno Setup)")
    cands = [os.environ.get(k, "") + "\\Inno Setup 6\\ISCC.exe" for k in ("ProgramFiles(x86)", "ProgramFiles")]
    cands.append(os.environ.get("LOCALAPPDATA", "") + "\\Programs\\Inno Setup 6\\ISCC.exe")
    iscc = next((p for p in cands if Path(p).exists()), None)
    if not iscc:
        sys.exit("Inno Setup 6 not found. Install: winget install JRSoftware.InnoSetup")
    run([iscc, "ZAIA_PDF.iss"])


if __name__ == "__main__":
    if os.name != "nt":
        sys.exit("run this on Windows")
    build_runtime()
    build_exe()
    build_setup()
    out = sorted((D / "Output").glob("ZAIA_PDF_Setup_*.exe"))[-1]
    print("\nDONE: %s (%.0f MB)" % (out, out.stat().st_size / 1e6))
