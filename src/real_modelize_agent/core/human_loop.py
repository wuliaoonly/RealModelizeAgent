from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


Intent = Literal["chat", "instruction", "task"]
PlanAction = Literal["none", "insert_plan", "execution_command"]

HUMAN_DIR = Path(".real-modelize") / "human-loop"
REQUEST_LOG = "requests.jsonl"


@dataclass(frozen=True)
class IntentDecision:
    intent: Intent
    target: str
    plan_action: PlanAction
    reason: str
    raw_text: str
    chart_style: dict[str, Any] = field(default_factory=dict)
    paragraph_edit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_CHAT_ONLY = re.compile(
    r"^\s*(你好|您好|hi|hello|嗨|谢谢|多谢|辛苦了|在吗|你是谁|聊聊|讲个笑话|"
    r"进度如何|现在怎么样|做得怎么样|有什么想法)[！!。.?？\s]*$",
    re.IGNORECASE,
)
_ACTION_WORDS = re.compile(
    r"(修改|改成|调整|替换|删除|新增|增加|插入|继续|执行|运行|重跑|重新|生成|"
    r"修复|完善|优化|编译|导出|写入|更新|补充|设置|采用|换成|请|帮我)"
)
_MODELING_WORDS = re.compile(r"(数学建模|数模|CUMCM|MCM|ICM|问题[一二三四五六七八九十\d]+|建立模型|求解)", re.IGNORECASE)
_CHART_WORDS = re.compile(r"(图表|图片|绘图|配色|色板|颜色|字号|fontsize|字体|中文显示|坐标轴|图例)", re.IGNORECASE)
_PAPER_WORDS = re.compile(r"(论文|段落|摘要|章节|小节|正文|latex|tex)", re.IGNORECASE)
_EXECUTION_WORDS = re.compile(r"(执行|运行|重跑|重新生成|编译|导出|画图|绘图|出图|修改论文|修改.*段)")


def classify_user_input(text: str, *, has_existing_workspace: bool = False) -> IntentDecision:
    """Deterministically classify an input before the model workflow is entered.

    Ambiguous inputs deliberately fall back to ``task`` for a new workspace and to
    ``instruction`` for a resumed workspace. This prevents a mutation request from
    being mistaken for casual chat.
    """
    raw = (text or "").strip()
    if not raw or _CHAT_ONLY.fullmatch(raw):
        return IntentDecision("chat", "peer_agents", "none", "matched casual-chat form", raw)

    chart_style = extract_chart_style(raw) if _CHART_WORDS.search(raw) else {}
    paragraph_edit = extract_paragraph_edit(raw) if _PAPER_WORDS.search(raw) else {}
    actionable = bool(_ACTION_WORDS.search(raw) or chart_style or paragraph_edit)

    if has_existing_workspace and actionable:
        target = "coderAgent" if chart_style else "writerAgent" if paragraph_edit else "planner"
        action: PlanAction = "execution_command" if _EXECUTION_WORDS.search(raw) or target != "planner" else "insert_plan"
        return IntentDecision("instruction", target, action, "action request for an existing workspace", raw, chart_style, paragraph_edit)

    if _MODELING_WORDS.search(raw) or len(raw) >= 80:
        return IntentDecision("task", "coordinator", "insert_plan", "new mathematical-modeling task", raw, chart_style, paragraph_edit)

    if actionable:
        return IntentDecision("instruction", "planner", "insert_plan", "explicit action request", raw, chart_style, paragraph_edit)

    return IntentDecision("chat", "peer_agents", "none", "no executable instruction detected", raw)


def extract_chart_style(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    size_match = re.search(r"(?:fontsize|字号|字体大小)\s*(?:设为|设置为|改为|改成|[:：=])?\s*(\d{1,2}(?:\.\d+)?)", text, re.IGNORECASE)
    if size_match:
        size = float(size_match.group(1))
        if 6 <= size <= 40:
            result["base_font_size"] = size

    named_palettes = {
        "蓝色": ["#2E5B88", "#5B8DB8", "#8FB9D8", "#D5E6F2"],
        "暖色": ["#C44E52", "#DD8452", "#E6B566", "#F2D7B6"],
        "冷色": ["#2E5B88", "#4A9B7F", "#64A6BD", "#A7D8D1"],
        "莫兰迪": ["#7D8F9B", "#A88F8F", "#8FA58F", "#C2B8A3"],
        "黑白": ["#222222", "#666666", "#AAAAAA", "#DDDDDD"],
    }
    for name, colors in named_palettes.items():
        if name in text:
            result["palette_name"] = name
            result["palette"] = colors
            break
    explicit = re.findall(r"#[0-9a-fA-F]{6}\b", text)
    if explicit:
        result["palette_name"] = "custom"
        result["palette"] = [item.upper() for item in explicit[:8]]

    font_match = re.search(r"(?:字体|font(?: family)?)\s*(?:设为|设置为|改为|改成|[:：=])\s*([\w\u4e00-\u9fff -]{2,40})", text, re.IGNORECASE)
    if font_match:
        result["font_family"] = font_match.group(1).strip(" ，,。")
    return result


def extract_paragraph_edit(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    section = re.search(
        r"(?:修改|调整|重写|润色)?\s*(?:论文(?:中|的)?)?\s*[《\"]?([^《》\"，,。]{2,30})[》\"]?\s*(?:章节|一节|小节)",
        text,
    )
    if section:
        result["section"] = section.group(1).strip()
    else:
        for known in ("摘要", "问题的提出和重述", "问题的分析", "模型假设", "符号说明", "数据的处理", "模型建立和求解", "模型的评价和改进", "模型评价", "模型的推广和应用", "参考文献", "附录"):
            if known in text:
                result["section"] = known
                break
    index = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*段", text)
    if index:
        result["paragraph"] = index.group(1)
    anchor = re.search(r"(?:包含|以|从)[“\"]([^”\"]{2,80})[”\"]", text)
    if anchor:
        result["anchor"] = anchor.group(1)
    if result or re.search(r"(段落|第.+段)", text):
        result["instruction"] = text.strip()
    return result


def record_human_request(workspace: Path, decision: IntentDecision) -> Path:
    root = workspace / HUMAN_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / REQUEST_LOG
    payload = {"timestamp": datetime.now(timezone.utc).isoformat(), **decision.to_dict()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return path
