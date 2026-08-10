from __future__ import annotations

from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage

from real_modelize_agent.providers.openai_provider import create_model


ROLE_PROMPTS = {
    "modelerAgent": "你是建模手，只做只读讨论：从模型选择、假设和验证角度回答，不创建或修改文件。",
    "coderAgent": "你是编程手，只做只读讨论：从算法、代码和图表角度回答，不执行命令、不修改文件。",
    "writerAgent": "你是论文手，只做只读讨论：从论证与写作角度回答，不修改论文。",
    "researchAgent": "你是研究手，只做只读讨论：说明还需什么证据，不联网、不编造来源。",
}


def peer_chat(question: str, *, writer: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Pull read-only replies from multiple specialist agents and synthesize them."""
    emit = writer or (lambda _: None)
    names = _select_roles(question)
    replies: list[dict[str, str]] = []
    for name in names:
        try:
            response = create_model().invoke([SystemMessage(content=ROLE_PROMPTS[name]), HumanMessage(content=question)])
            content = str(getattr(response, "content", "") or "").strip()
        except Exception as exc:
            content = f"{name} 暂时无法回复：{type(exc).__name__}"
        item = {"agent": name, "content": content}
        replies.append(item)
        emit({"type": "peer_reply", **item})
    joined = "\n\n".join(f"[{item['agent']}] {item['content']}" for item in replies)
    try:
        response = create_model().invoke(
            [
                SystemMessage(content="你是数模团队协调员。综合各专家只读意见，直接、简洁地回复用户；不要声称执行了修改。"),
                HumanMessage(content=f"用户：{question}\n\n专家意见：\n{joined}"),
            ]
        )
        final = str(getattr(response, "content", "") or "").strip()
    except Exception:
        final = joined
    return {"replies": replies, "final_answer": final}


def _select_roles(question: str) -> list[str]:
    selected: list[str] = []
    mappings = [
        ("coderAgent", ("代码", "图", "算法", "运行")),
        ("writerAgent", ("论文", "写", "段落", "摘要")),
        ("modelerAgent", ("模型", "假设", "方法", "建模")),
        ("researchAgent", ("资料", "文献", "依据", "来源")),
    ]
    for role, words in mappings:
        if any(word in question for word in words):
            selected.append(role)
    return selected[:2] or ["modelerAgent", "writerAgent"]
