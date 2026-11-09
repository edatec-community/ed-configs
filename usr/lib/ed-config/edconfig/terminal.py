from __future__ import annotations

import os
from typing import Any


_COLORS = {"green": "32", "yellow": "33"}


def colorize(message: str, color: str, stream: Any) -> str:
    code = _COLORS.get(color)
    if (
        code is None
        or "NO_COLOR" in os.environ
        or os.environ.get("TERM") == "dumb"
        or not hasattr(stream, "isatty")
        or not stream.isatty()
    ):
        return message
    return f"\033[{code}m{message}\033[0m"
