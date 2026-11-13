"""Images attached to a user message.

An image is attached by mentioning its file in the message: `@shot.png`, a
plain path, or a path the terminal inserted when a file was dragged in. The
clipboard can be pasted too, where the platform offers a tool to read it.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .branding import app_dir

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 5_000_000
MAX_IMAGES = 8
# A quoted path, or a run of non-space characters where `\ ` is an escaped space.
_TOKEN = re.compile(r"""'([^']+)'|"([^"]+)"|((?:\\.|[^\s\\])+)""")


def find_images(text: str, workspace: Path) -> tuple[list[Path], list[str]]:
    """Image files mentioned in `text`. Returns (images, problems worth telling the user)."""
    images: list[Path] = []
    problems: list[str] = []
    for match in _TOKEN.finditer(text):
        token = next(group for group in match.groups() if group)
        token = re.sub(r"\\(.)", r"\1", token).lstrip("@").rstrip(",.;:!?)")
        if Path(token).suffix.lower() not in IMAGE_TYPES:
            continue
        path = Path(token).expanduser()
        path = path if path.is_absolute() else workspace / path
        if not path.is_file():
            continue
        if path in images:
            continue
        if path.stat().st_size > MAX_IMAGE_BYTES:
            problems.append(f"{path.name}: larger than {MAX_IMAGE_BYTES // 1_000_000} MB")
        elif len(images) >= MAX_IMAGES:
            problems.append(f"{path.name}: at most {MAX_IMAGES} images per message")
        else:
            images.append(path)
    return images, problems


def user_content(text: str, images: list[Path]) -> str | list[dict[str, Any]]:
    """The content of a user message: plain text, or text plus image parts."""
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "input_text", "text": text}]
    for path in images:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        mime = IMAGE_TYPES[path.suffix.lower()]
        parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}", "detail": "auto"})
    return parts


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(command, capture_output=True, timeout=5, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        return None


def clipboard_image(directory: Path | None = None) -> Path | None:
    """Save the image on the clipboard to a file, if there is one and we can read it."""
    directory = directory or app_dir() / "pasted"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    target = directory / f"paste-{time.strftime('%Y%m%d-%H%M%S')}.png"
    if sys.platform == "darwin":
        script = (
            'set f to open for access POSIX file "%s" with write permission\n'
            "try\n write (the clipboard as «class PNGf») to f\n close access f\n"
            "on error\n close access f\n error number 1\nend try" % target
        )
        done = _run(["osascript", "-e", script])
        ok = done is not None and done.returncode == 0
    elif os.name == "nt":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$i=[Windows.Forms.Clipboard]::GetImage();"
            f"if($i){{$i.Save('{target}',[Drawing.Imaging.ImageFormat]::Png)}}else{{exit 1}}"
        )
        done = _run(["powershell", "-NoProfile", "-Command", script])
        ok = done is not None and done.returncode == 0
    else:
        ok = False
        for command in (["wl-paste", "--type", "image/png"],
                        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):  # fmt: skip
            if shutil.which(command[0]) is None:
                continue
            done = _run(command)
            if done is not None and done.returncode == 0 and done.stdout.startswith(b"\x89PNG"):
                target.write_bytes(done.stdout)
                ok = True
                break
    if ok and target.is_file() and target.stat().st_size > 0:
        return target
    target.unlink(missing_ok=True)
    return None
