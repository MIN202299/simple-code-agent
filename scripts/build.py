#!/usr/bin/env python3
"""Build a standalone executable with PyInstaller.

    pip install -e ".[build]"
    python scripts/build.py            # one file:   dist/nori  (dist/nori.exe on Windows)
    python scripts/build.py --onedir   # a folder:   dist/nori/ (starts faster)

The result runs without Python installed. It is built for the OS and CPU you
run this on; build on each platform you want to ship.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "nori"
# Optional extras of the SDK that are never used here; leaving them out keeps the
# executable smaller and avoids import errors for their missing dependencies.
EXCLUDES = ("agents.voice", "mcp.cli", "tkinter")
# Distributions whose version is looked up through importlib.metadata at runtime.
METADATA = ("mcp", "openai-agents", "openai")
# Packages that read their own non-Python files (prompt templates and the like).
DATA = ("agents", "mcp", "openai")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--onedir", action="store_true", help="build a folder instead of one file")
    args = parser.parse_args()

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", NAME,
        "--console",
        "--onedir" if args.onedir else "--onefile",
        "--paths", str(ROOT / "src"),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]  # fmt: skip
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    for distribution in METADATA:
        command += ["--copy-metadata", distribution]
    for package in DATA:
        command += ["--collect-data", package]
    command.append(str(ROOT / "scripts" / "entry.py"))

    print("$", " ".join(command))
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    suffix = ".exe" if platform.system() == "Windows" else ""
    binary = ROOT / "dist" / (NAME if not args.onedir else f"{NAME}/{NAME}")
    binary = binary.with_name(binary.name + suffix)
    check = subprocess.run([str(binary), "--version"], capture_output=True, text=True)
    if check.returncode != 0:
        print(f"built {binary}, but it failed to start:\n{check.stdout}{check.stderr}")
        return 1
    size = binary.stat().st_size / 1e6 if not args.onedir else sum(
        f.stat().st_size for f in binary.parent.rglob("*") if f.is_file()
    ) / 1e6
    print(f"\nbuilt {binary} ({size:.0f} MB): {check.stdout.strip()}")
    if shutil.which(NAME) is None:
        print(f"copy it somewhere on your PATH, e.g. /usr/local/bin/{NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
