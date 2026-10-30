r"""Build requirements.lock: the runtime packages of LectureLab at the versions in the tested .venv,
each with every sha256 PyPI publishes for that version. yt-dlp is left out on purpose (it floats).
Run it from the repo root for each release, with the tested .venv:
    .venv\Scripts\python tools\make_lock.py <an empty scratch folder>"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
work = Path(sys.argv[1])
work.mkdir(parents=True, exist_ok=True)
py313 = sys.executable  # the tested .venv's python: its base is the same 3.13

frozen = subprocess.run([py313, "-m", "pip", "freeze", "--all", "--exclude-editable"], capture_output=True, text=True,
                        check=True).stdout
constraints = work / "tested.txt"
constraints.write_text("\n".join(l for l in frozen.splitlines() if "==" in l) + "\n")

venv = work / "closure-venv"
subprocess.run([py313, "-m", "venv", "--clear", str(venv)], check=True)
vpy = venv / "Scripts" / "python.exe"
subprocess.run([vpy, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-c", str(constraints),
                str(repo), "setuptools"], check=True)
lines = subprocess.run([vpy, "-m", "pip", "freeze", "--all"], capture_output=True, text=True, check=True).stdout
skip = {"pip", "lecturelab", "yt-dlp", "yt_dlp"}
pins = []
for l in lines.splitlines():
    name, _, ver = l.partition("==")
    if ver and name.lower().replace("_", "-") not in skip:
        pins.append((name, ver))

out = ["# Exact runtime packages for LectureLab, as tested, with PyPI's sha256 for each file.",
       "# setup.ps1 installs these with --require-hashes, so a package swapped on PyPI later won't install.",
       "# yt-dlp isn't here: it floats, because sites break old versions. Regenerate this file for each release.", ""]
for name, ver in sorted(pins, key=lambda p: p[0].lower()):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=60) as r:
        data = json.load(r)
    files = [u for u in data["urls"] if not u.get("yanked")]
    # setup.ps1 installs wheels only (--only-binary): each package needs one that runs on 64-bit Windows
    if not any(u["filename"].endswith(("-win_amd64.whl", "-none-any.whl")) for u in files):
        raise SystemExit(f"{name}=={ver} has no wheel for 64-bit Windows on PyPI")
    hashes = sorted({u["digests"]["sha256"] for u in files})
    out.append(f"{name}=={ver} \\")
    out += [f"    --hash=sha256:{h} \\" for h in hashes[:-1]] + [f"    --hash=sha256:{hashes[-1]}"]
(repo / "requirements.lock").write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"{len(pins)} packages locked")
