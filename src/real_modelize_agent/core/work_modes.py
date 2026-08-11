"""Explicit work modes used to route tools and acceptance checks."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


class CodeWorkType(StrEnum):
    MODEL = "model"
    FIGURE = "figure"


_FIGURE_PATTERN = re.compile(
    r"(?:figure|figures|plot|chart|visuali[sz]|图表|绘图|作图|修图|配色|dpi|字体)",
    re.IGNORECASE,
)


def resolve_code_work_type(instruction: str, state: dict[str, Any] | None = None) -> CodeWorkType:
    """Let a concrete figure delegation override stale state from an earlier call."""
    if _FIGURE_PATTERN.search(instruction or ""):
        return CodeWorkType.FIGURE
    explicit = str((state or {}).get("code_work_type", "")).strip().lower()
    if explicit in {mode.value for mode in CodeWorkType}:
        return CodeWorkType(explicit)
    return CodeWorkType.MODEL
