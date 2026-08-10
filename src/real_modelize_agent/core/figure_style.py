from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from PIL import Image


STYLE_PATH = Path(".real-modelize") / "human-loop" / "figure-style.json"
DEFAULT_PALETTE = ["#2E5B88", "#E85D4C", "#4A9B7F", "#7F7F7F", "#B8D4E8"]
DEFAULT_STYLE: dict[str, Any] = {
    "font_family": "Microsoft YaHei",
    "font_candidates": ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"],
    "base_font_size": 11.0,
    "title_font_size": 12.0,
    "label_font_size": 11.0,
    "tick_font_size": 10.0,
    "legend_font_size": 10.0,
    "palette_name": "academic",
    "palette": DEFAULT_PALETTE,
    "dpi": 300,
    "svg_text_as_text": True,
}


def normalize_figure_style(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    style = {**DEFAULT_STYLE}
    style["font_candidates"] = list(DEFAULT_STYLE["font_candidates"])
    style["palette"] = list(DEFAULT_STYLE["palette"])
    for key, value in (overrides or {}).items():
        if key.endswith("font_size"):
            number = float(value)
            if not 6 <= number <= 40:
                raise ValueError(f"{key} must be between 6 and 40")
            style[key] = number
        elif key == "palette":
            colors = [str(color).upper() for color in value]
            if not 2 <= len(colors) <= 12 or any(not re.fullmatch(r"#[0-9A-F]{6}", color) for color in colors):
                raise ValueError("palette must contain 2..12 #RRGGBB colors")
            style[key] = colors
        elif key in style:
            style[key] = value
    base = float(style["base_font_size"])
    if overrides and "base_font_size" in overrides:
        derived = {
            "title_font_size": max(base + 1, base),
            "label_font_size": base,
            "tick_font_size": max(6, base - 1),
            "legend_font_size": max(6, base - 1),
        }
        for key, value in derived.items():
            if key not in overrides:
                style[key] = value
    return style


def save_figure_style(workspace: Path, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    path = workspace / STYLE_PATH
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            existing = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError):
            existing = {}
    merged = {**existing, **(overrides or {})}
    if overrides and "base_font_size" in overrides:
        for key in ("title_font_size", "label_font_size", "tick_font_size", "legend_font_size"):
            if key not in overrides:
                merged.pop(key, None)
    style = normalize_figure_style(merged)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(style, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": STYLE_PATH.as_posix(), "style": style}


def load_figure_style(workspace: Path) -> dict[str, Any]:
    path = workspace / STYLE_PATH
    if not path.exists():
        return normalize_figure_style()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return normalize_figure_style(raw if isinstance(raw, dict) else {})


def apply_matplotlib_style(style: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply a CJK-safe style inside generated solver scripts.

    Imports are lazy, so the core package still works when modeling extras are not
    installed. Generated scripts should call this before creating any figure.
    """
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    resolved = normalize_figure_style(style)
    installed = {item.name for item in font_manager.fontManager.ttflist}
    cjk_family = next((name for name in resolved["font_candidates"] if name in installed and name != "DejaVu Sans"), None)
    if cjk_family is None:
        raise RuntimeError(
            "No CJK-capable font was found. Install Microsoft YaHei, SimHei, Noto Sans CJK SC, or Source Han Sans SC."
        )
    resolved["resolved_cjk_font"] = cjk_family
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": [resolved["font_family"], cjk_family, *resolved["font_candidates"]],
            "font.size": resolved["base_font_size"],
            "axes.titlesize": resolved["title_font_size"],
            "axes.labelsize": resolved["label_font_size"],
            "xtick.labelsize": resolved["tick_font_size"],
            "ytick.labelsize": resolved["tick_font_size"],
            "legend.fontsize": resolved["legend_font_size"],
            "axes.unicode_minus": False,
            "figure.dpi": resolved["dpi"],
            "savefig.dpi": resolved["dpi"],
            "savefig.bbox": "tight",
            "svg.fonttype": "none",
        }
    )
    return resolved


def audit_figure_workspace(workspace: Path) -> dict[str, Any]:
    style = load_figure_style(workspace)
    figures = sorted(workspace.glob("problem*/图表/**/*.png"))
    scripts = sorted(workspace.glob("problem*/代码/*.py"))
    checks: list[dict[str, Any]] = []

    source = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in scripts)
    style_hook = "apply_matplotlib_style" in source
    manual_cjk = "font.sans-serif" in source and "axes.unicode_minus" in source
    checks.append(
        {
            "name": "中文字体配置",
            "passed": bool(scripts) and (style_hook or manual_cjk),
            "detail": "已调用统一样式模块" if style_hook else "检测到手工 CJK 配置" if manual_cjk else "求解脚本未配置中文字体回退与负号显示",
        }
    )

    requested_colors = [color.upper() for color in style["palette"]]
    source_upper = source.upper()
    palette_ok = style_hook or all(color in source_upper for color in requested_colors[:2])
    checks.append({"name": "整体配色", "passed": palette_ok, "detail": f"palette={requested_colors}"})

    image_failures: list[str] = []
    for path in figures:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                dpi = image.info.get("dpi", (0, 0))
                if min(image.size) < 300 or (dpi and min(dpi) < 250):
                    image_failures.append(path.relative_to(workspace).as_posix())
        except Exception:
            image_failures.append(path.relative_to(workspace).as_posix())
    checks.append(
        {
            "name": "图片有效性与清晰度",
            "passed": bool(figures) and not image_failures,
            "detail": f"有效 PNG {len(figures)} 张" if figures and not image_failures else f"不合格: {image_failures[:8]}",
        }
    )
    return {
        "ok": all(check["passed"] for check in checks),
        "style_path": STYLE_PATH.as_posix(),
        "style": style,
        "checks": checks,
        "figures": [path.relative_to(workspace).as_posix() for path in figures],
    }
