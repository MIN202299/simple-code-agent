"""Small helpers with no heavy imports, safe to use at start-up."""

from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_CONTEXT_WINDOW = 128_000
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Expand `${VAR}` and `${VAR:-default}` in strings, recursively."""
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value
