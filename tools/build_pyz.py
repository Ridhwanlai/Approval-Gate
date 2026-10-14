#!/usr/bin/env python3
"""Build dist/para.pyz: the whole product as one file that runs with `python3 para.pyz`.

The archive holds the para package, the persona page, the firm's citation lists, the harness
specs and the mock Ezio. It never holds a key, a registry or any log. On first launch it unpacks
itself into ~/.para/app/<version>/ (PARA_HOME overrides ~/.para) and runs para.cli from there.

  python3 tools/build_pyz.py              # -> dist/para.pyz
  python3 tools/build_pyz.py --out PATH
"""
import argparse
import hashlib
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["para", "ui", "firm", "mock_ezio.py", "fixtures.json"]
SPECS = "harnesses/para-*.json"

BOOTSTRAP = '''\
"""Para, packaged. Unpacks once into PARA_HOME/app/<version>/ and runs from there."""
import os
import sys
import zipfile
from pathlib import Path

VERSION = "{version}"

if sys.version_info < (3, 9):
    sys.exit("Para needs Python 3.9 or newer.")
home = Path(os.environ.setdefault("PARA_HOME", str(Path.home() / ".para")))
app = home / "app" / VERSION
if not (app / ".complete").exists():
    staging = home / "app" / (VERSION + ".partial")
    with zipfile.ZipFile(os.path.dirname(os.path.abspath(__file__))) as z:
        names = [n for n in z.namelist() if n != "__main__.py"]
        z.extractall(staging, names)
    (staging / ".complete").write_text(VERSION)
    if app.exists():
        import shutil
        shutil.rmtree(app)
    staging.rename(app)
sys.path.insert(0, str(app))
from para.cli import main  # noqa: E402

main()
'''


def build(out):
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "para"
        stage.mkdir()
        for name in INCLUDE:
            src = ROOT / name
            if src.is_dir():
                shutil.copytree(src, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
            else:
                shutil.copy2(src, stage / name)
        (stage / "harnesses").mkdir()
        for spec in sorted(ROOT.glob(SPECS)):
            shutil.copy2(spec, stage / "harnesses" / spec.name)
        digest = hashlib.sha256()
        for f in sorted(p for p in stage.rglob("*") if p.is_file()):
            digest.update(str(f.relative_to(stage)).encode() + b"\0" + f.read_bytes())
        version = digest.hexdigest()[:12]
        (stage / "__main__.py").write_text(BOOTSTRAP.format(version=version))
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(stage, out, interpreter="/usr/bin/env python3", compressed=True)
    return version


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / "para.pyz")
    args = ap.parse_args()
    version = build(args.out)
    print(f"Built {args.out} ({args.out.stat().st_size // 1024} KB, version {version})")


if __name__ == "__main__":
    main()
