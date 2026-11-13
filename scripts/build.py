#!/usr/bin/env python3
"""Build a standalone executable with PyInstaller.

    pip install -e ".[build]"
    python scripts/build.py              # one file:  dist/nori  (dist/nori.exe on Windows)
    python scripts/build.py --onedir     # a folder:  dist/nori/ (starts faster)
    python scripts/build.py --archive nori-linux-x86_64
                                         # the folder, packed as dist/<name>.tar.gz (.zip on Windows)

The result runs without Python installed. It is built for the OS and CPU you
run this on; build on each platform you want to ship.

The one-file build unpacks itself on every start, which costs a few seconds.
The folder build does not, so that is what the release archives and the
Homebrew formula use.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "nori"
WINDOWS = platform.system() == "Windows"
# Optional extras of the SDK that are never used here; leaving them out keeps the
# executable smaller and avoids import errors for their missing dependencies.
EXCLUDES = ("agents.voice", "mcp.cli", "tkinter")
# Distributions whose version is looked up through importlib.metadata at runtime.
METADATA = ("mcp", "openai-agents", "openai")
# Packages that read their own non-Python files (prompt templates and the like).
DATA = ("agents", "mcp", "openai")


def build(onedir: bool) -> Path:
    """Run PyInstaller and return the path of the executable."""
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--console",
        "--onedir" if onedir else "--onefile",
        "--paths", str(ROOT / "src"),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]  # fmt: skip
    if platform.system() == "Linux":
        command.append("--strip")  # debug symbols are a third of the size there
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    for distribution in METADATA:
        command += ["--copy-metadata", distribution]
    for package in DATA:
        command += ["--collect-data", package]
    command.append(str(ROOT / "scripts" / "entry.py"))

    print("$", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)
    suffix = ".exe" if WINDOWS else ""
    directory = ROOT / "dist" / NAME if onedir else ROOT / "dist"
    return directory / f"{NAME}{suffix}"


def check(binary: Path) -> tuple[str, float]:
    """Start the executable once; returns its version line and how long it took."""
    subprocess.run([str(binary), "--version"], capture_output=True)  # warm the file cache
    started = time.perf_counter()
    done = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
    elapsed = time.perf_counter() - started
    if done.returncode != 0:
        raise SystemExit(f"built {binary}, but it failed to start:\n{done.stdout}{done.stderr}")
    return done.stdout.strip(), elapsed


def archive(folder: Path, name: str) -> Path:
    """Pack the build folder as <name>.tar.gz (.zip on Windows), with `nori/` at the top."""
    if WINDOWS:
        target = ROOT / "dist" / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as bundle:
            for file in sorted(folder.rglob("*")):
                bundle.write(file, Path(NAME) / file.relative_to(folder))
    else:
        target = ROOT / "dist" / f"{name}.tar.gz"
        with tarfile.open(target, "w:gz") as bundle:
            bundle.add(folder, arcname=NAME)
    return target


def megabytes(path: Path) -> float:
    files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]
    return sum(f.stat().st_size for f in files) / 1e6


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onedir", action="store_true", help="build a folder instead of one file")
    parser.add_argument("--archive", metavar="NAME", help="build a folder and pack it as dist/NAME")
    args = parser.parse_args()
    onedir = args.onedir or bool(args.archive)

    try:
        binary = build(onedir)
    except subprocess.CalledProcessError as exc:
        return exc.returncode
    version, elapsed = check(binary)
    built = binary.parent if onedir else binary
    print(f"\nbuilt {built} ({megabytes(built):.0f} MB): {version}, starts in {elapsed:.2f}s")
    if args.archive:
        packed = archive(binary.parent, args.archive)
        print(f"packed {packed} ({megabytes(packed):.0f} MB)")
    elif shutil.which(NAME) is None:
        print(f"copy it somewhere on your PATH, e.g. /usr/local/bin/{NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
