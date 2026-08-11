# RealModelizeAgent 架构文档

> 数学建模 Code Agent：输入一道数模竞赛题（CUMCM/MCM），端到端生成
> `题目分析.md` → `建模方案.json` → 求解代码 / `Pictures/` / `结果/` → `论文.tex` / `论文.pdf`。
>
> 本文档基于 **2026-08-11** 的代码状态编写，并附「已实现 / 未实现」模块清单。

---

## 1. 项目概览

| 项                            | 说明                                                                                                          |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------- |
| 包名                          | `real-modelize-agent` v0.1.0                                                                                |
| 语言                          | Python ≥ 3.11                                                                                                |
| 核心依赖                      | `langchain` / `langchain-openai` / `langgraph` / `typer` / `rich` / `textual` / `tavily-python` |
| 科学计算（extra`modeling`） | numpy / pandas / matplotlib / scipy / scikit-learn / statsmodels / seaborn                                    |
| 入口                          | `real-modelize`（pyproject `[project.scripts]` → `cli.app:app`）                                       |
| 是否 git 仓库                 | 是（GitHub: `wuliaoonly/RealModelizeAgent`）                                                              |
| 测试                          | `uv run pytest` → **58 passed**（2026-08-11 实测）                                                   |

---

## 2. 顶层架构

```
                     ┌────────────────────────────────────────────────────┐
                     │                    CLI 层                          │
                     │  cli/app.py(Typer 入口/compile/dry-run)            │
                     │  cli/formatter.py(Rich 事件渲染)                    │
                     │  cli/event_summary.py(TUI 摘要)                    │
                     │  cli/tui/  (Textual TUI / 审批弹窗 / 蓝色三角 logo) │
                     └───────────────────────┬────────────────────────────┘
                                             │ stream_agent_events()
                     ┌───────────────────────▼────────────────────────────┐
                     │                 core/ 运行时编排                     │
                     │  agent.py(事件流主循环)  human_loop.py(入口意图)    │
                     │  checkpoint.py(断点/恢复)  trace.py(轨迹)           │
                     │  session.py(会话)  approval.py(审批)  state.py      │
                     │  paths.py(路径)  figure_style.py(图表样式)          │
                     └───────────────────────┬────────────────────────────┘
                                             │ workflow.stream(inputs)
                     ┌───────────────────────▼────────────────────────────┐
                     │           graph/  LangGraph 状态图                 │
                     │  workflow.py(entry+complex 两图)                    │
                     │  nodes.py(coordinator/planner/verifier/…/final)     │
                     │  memory.py(分层记忆)  state.py(GraphState)          │
                     └──────┬───────────┬────────────┬───────────┬────────┘
                            │           │            │           │
                     ┌──────▼──┐  ┌─────▼─────┐  ┌───▼─────┐  ┌─▼─────────┐
                     │ 建模手   │  │ 编程手     │  │ 论文手   │  │ 研究手    │
                     │modeler   │  │code_agent │  │write     │  │research   │
                     │_agent.py │  │.py        │  │_agent.py │  │_agent.py  │
                     └────┬─────┘  └─────┬─────┘  └────┬────┘  └─────┬─────┘
                          │   ReAct 循环（bind_tools + 执行）            │
                          │   chat_agent.py（闲聊：多专家只读汇总）       │
                     ┌────▼──────────────────▼──────────▼───────────────▼──┐
                     │                tools/  工具层                        │
                     │  registry(工具组)  bash / file / grep / notepad      │
                     │  / todo / paper_edit / docx / xlsx / pdf / office    │
                     │  / paper_search（学术检索）/ assets（算法知识库）     │
                     └───────────────────┬──────────────────────────────────┘
                                         │ create_model()
                     ┌───────────────────▼──────────────────────────────────┐
                     │          providers/openai_provider.py                │
                     │          OpenAI 兼容工厂（API_KEY/MODEL/BASE_URL）   │
                     └──────────────────────────────────────────────────────┘
```

## 3. 工作流（两段式）

### 3.1 Entry 图：数模题判定

```
START → coordinator（LLM 判定 + 结构化 JSON）
             │
     detected? ├─ 否 → refuse → END（输出"非数模题"）
              └─ 是 → END（事件流随后进入 complex 图）
```

### 3.2 Complex 图：plan→execute→verify 循环

```
START → planner（规划 + 委派建模手/编程手/论文手/研究手）
          → context_monitor（估算 token，决定是否压缩）
              │
     超限？ ├─ 是 → context_compressor（模型压缩 → 历史摘要持久化）
              │          │
              └──────────▼
            verifier（只读工具检查工作区 → 返回 JSON 验收）
              │
      passed？├─ 是 → final → END（PASSED）
              │
      尝试耗尽？├─ 是 → final → END（FAILED）
              └─ 否 → 回 planner（仅委派缺失部分修复）
```

- **planner** 是 supervisor：`TodoWriteTool` 发布计划 + `CallModelerAgentTool` / `CallCoderAgentTool` / `CallWriterAgentTool` / `CallResearchAgentTool` 委派专家（`graph/nodes.py:_build_planner_tools`）。
- **专家 Agent**（建模手/编程手/论文手）各自是 ReAct 循环，在 `agents/*.py` 内用 `bind_tools` + 自建消息循环执行（非独立 LangGraph 节点，是 planner 的“工具”）。
- **verifier** 绑定只读工具（读/搜/bash/便签/搜索），只返回 JSON：`{passed, reason, checks[], recommended_next_instruction}`。

---

## 4. 图状态（`graph/state.py`）

`RealModelizeGraphState`（TypedDict，`messages` 用 `add_messages` 归约）字段分组：

| 分组        | 字段                                                                                                                                                               |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 任务/运行时 | `task` `runtime` `messages`                                                                                                                                  |
| coordinator | `problem_detected` `problem_json` `coordinator_reason`                                                                                                       |
| 计划        | `plan_summary` `todos` `acceptance_criteria` `verification_commands`                                                                                       |
| 验证        | `verification_results/checks` `verifier_summary` `passed` `attempts` `max_attempts` `last_error` `final_answer`                                      |
| 建模手      | `modeler_summary` `modeler_plan` `modeler_plan_path`                                                                                                         |
| 编程手      | `coder_summary` `figures` `results_summary`                                                                                                                  |
| 论文手      | `writer_summary` `paper_path` `paper_compile_ok`                                                                                                             |
| 研究手      | `research_notes` `sources` `agent_handoffs`                                                                                                                  |
| 上下文工程  | `context_summary` `context_token_count/limit` `context_should_compress` `context_next_node` `compression_events` `memory_snapshot` `history_summary` |
| 会话        | `session_id` `session_turn` `session_context` ⚠️（见 §7.2）                                                                                               |
| 杂项        | `metadata`                                                                                                                                                       |

---

## 5. 已实现模块清单（✅ 完整实现并接线）

| 模块       | 文件                             | 职责                                                                                                    | 状态                         |
| ---------- | -------------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------- |
| 运行时编排 | `core/agent.py`                | `create_runtime` + `stream_agent_events`：入口判定 → complex 图 → checkpoint/trace 全生命周期接线 | ✅                           |
| 断点/恢复  | `core/checkpoint.py`           | light/strict/off 三模式、RECOVERY.md、workspace 清单、git 快照、`--resume` 恢复                       | ✅ 有测试                    |
| 审批       | `core/approval.py`             | inline/auto/deny 三模式 + 高危命令风险规则（pip/uv/npm/curl/uvicorn…）                                 | ✅ 已接入 bash_tool          |
| Trace      | `core/trace.py`                | `events.jsonl` + `summary.json` + `timeline.md`，统计节点访问/工具调用/审批/断点                  | ✅ 已接线                    |
| 路径       | `core/paths.py`                | 项目根探测、`workspace-*` 默认工作区                                                                  | ✅                           |
| 图编排     | `graph/workflow.py`            | entry + complex 两图                                                                                    | ✅ 有编译测试                |
| 图节点     | `graph/nodes.py`               | coordinator/refuse/planner/verifier/context_monitor/context_compressor/final + 路由                     | ✅ 有测试                    |
| 分层记忆   | `graph/memory.py`              | rules / working_memory / history_summary_store 三层 + HISTORY_SUMMARY.md 持久化                         | ✅                           |
| 建模手     | `agents/modeler_agent.py`      | 产出`题目分析.md` + `建模方案.json`（无 Bash，不跑代码）                                            | ✅ 有 e2e 假链路测试         |
| 编程手     | `agents/code_agent.py`         | 逐问求解 `problem{i}/代码` + `图表` + `结果` + NOTEPAD.md 关键数字                                 | ✅ 有 e2e 假链路测试         |
| 论文手     | `agents/write_agent.py`        | 数模论文模板 seed → 就地填充 `论文.tex` + xelatex 编译 + 图表轮逐问把关                               | ✅ 有测试（含 compile 探针） |
| 研究手     | `agents/research_agent.py`     | Tavily 联网检索 + 论文检索，仅持久化真实结果与 BibTeX；未配 key 优雅跳过                              | ✅                           |
| 团队闲聊   | `agents/chat_agent.py`         | 入口识别为闲聊时，多专家只读回复汇总后直答，不执行修改                                               | ✅ 已接线                    |
| 入口意图   | `core/human_loop.py`           | `classify_user_input`(chat/instruction/task) + 请求记录 + 定向修改意图解析                           | ✅ 已接线                    |
| 论文段落编辑 | `tools/paper_edit_tool.py`   | 定位 LaTeX 章节内唯一段落，替换正文并记录 sha256（编译失效标记）                                      | ✅ 已接线                    |
| 学术检索   | `tools/paper_search/`          | OpenAlex + AnySearch 双引擎论文检索，SKILL 驱动                                                  | ✅ 已接线                    |
| Office 工具 | `tools/office/`（docx/xlsx/pdf 共享） | soffice 转换/拆包/打包/校验/重算，沙箱 AF_UNIX shim                                        | ✅ 已接线                    |
| 算法知识库 | `assets/` + `tools/algorithm_lookup.py` | 0X-*.md 算法说明 + 按题型速查表，注入建模手简报                                    | ✅ 已接线                    |
| 工具注册   | `tools/registry.py`            | 全工具/只读/建模手/编程手/论文手 五套工具组                                                             | ✅                           |
| Shell      | `tools/bash_tool.py`           | Windows cmd 适配、超时/输出截断、危险命令拦截、后台执行、.env 加载、python/pip shim                     | ✅                           |
| 文件       | `tools/file_tools.py`          | 读(offset/limit)/写/编辑，mtime 读前校验、workspace 越界防护、unified diff                              | ✅                           |
| 搜索       | `tools/grep_tool.py`           | 正则全文搜索                                                                                            | ✅                           |
| 便签       | `tools/notepad_tool.py`        | NOTEPAD.md 读/追加（跨 Agent 持久笔记）                                                                 | ✅                           |
| 任务       | `tools/todo_tool.py`           | TODO.md 渲染、状态机(pending/in_progress/completed/blocked)、持久化                                     | ✅                           |
| 联网搜索   | `tools/paper_search/web_search_tool.py` | Tavily WebSearchTool（随 paper_search 包移动）                                               | ✅                           |
| 提示词     | `prompts/*.py`                 | multiAgent/planExecute/model/code_figure/write/contextCompression 六组                                  | ✅ 主要提示词均接线          |
| 模型工厂   | `providers/openai_provider.py` | OpenAI 兼容`ChatOpenAI` 工厂                                                                          | ✅                           |
| CLI 入口   | `cli/app.py`                   | 主命令 /`tui` / `compile` / `--dry-run`                                                           | ✅ 有测试                    |
| Rich 渲染  | `cli/formatter.py`             | 各类事件 Panel 渲染（intent_decision bug 已修，死分支已删）                                          | ✅                           |
| TUI 主界面 | `cli/tui/app.py`               | Textual 界面（事件滚动 + 输入框 + inline 审批弹窗 + 蓝色三角 logo）                                   | ✅ 已接线                    |
| TUI 摘要   | `cli/event_summary.py`         | 事件→一行摘要                                                                                          | ✅                           |
| 冒烟脚本   | `scripts/smoke_test.py`        | 脚本化模型假跑完整链路，打印 PASS/FAIL                                                                  | ✅                           |
| 测试       | `tests/*.py`                   | coordinator/planner/verifier/context/paper/human_loop/cli/e2e 共**58 项，全部通过**                 | ✅                           |

---

## 6. 关键机制

### 6.1 Checkpoint（light / strict / off）

- 每次图更新或关键事件后保存 `checkpoint.json`，light 模式仅存摘要 + RECOVERY.md；strict 额外存 `state.json`（序列化 messages）与 `events.jsonl`。
- 每次保存用工作区内的独立 git 仓（`.real-modelize/checkpoints/git`）做快照。
- `--resume <workspace>`：light 用 RECOVERY.md + TODO/NOTEPAD/HISTORY 重建上下文；strict 直接反序列化完整状态。
- 参考实现：[checkpoint.py](src/real_modelize_agent/core/checkpoint.py)。

### 6.2 分层记忆

- 规则层（写死，约束 agent 行为）→ 工作记忆（task/todos/sources/handoffs/summaries）→ 历史摘要仓（HISTORY_SUMMARY.md + NOTEPAD.md + 压缩事件）。
- 由运行时在 planner/verifier/context_compressor 各节点快照注入 prompt，agent 无记忆写工具。

### 6.3 上下文压缩

- `context_monitor` 用 `get_num_tokens_from_messages` 估算，超过 `RMA_CONTEXT_TOKEN_LIMIT`(默认 40 万) 触发 `context_compressor`：LLM 压缩 → 清空 messages → 只留一条摘要消息 → 写 HISTORY_SUMMARY.md。

### 6.4 审批

- `bash_tool` 对风险命令（pip install、curl 下载、起服务等）按模式审批：inline→CLI 询问 / **TUI 弹窗（已接线）**；auto→放行；deny→拒绝。
- TUI 侧：工作线程 `_make_approval_handler` 发 `ApprovalRequestedMessage` → 主线程 `push_screen(ApprovalModal)` → 回调 `ApprovalGate.resolve` → 阻塞 `wait` 返回；弹窗被关闭默认拒绝。

---

## 7. 未实现 / 已声明未落地清单（❌ / ⚠️）

| # | 项                           | 位置                                                   | 现状              | 说明                                                                                                                                                                                                                                  |
| - | ---------------------------- | ------------------------------------------------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **LaTeX 引擎可配置**   | `RMA_LATEX_ENGINE` + `write_agent.latex_engine()`  | ✅ FIXED         | `write_agent` 与 `cli/app.py compile` 均读取 `RMA_LATEX_ENGINE`（默认 xelatex，国赛模板仅支持 XeLaTeX）。                                                                                                            |
| 2 | **多轮会话**           | `core/agent.py`（session 生命周期）                  | ✅ FIXED         | `stream_agent_events` 已接线 `load_or_create_session` / `append_user_turn` / `save_session`，并发射 session_started/turn/saved 事件。                                                              |
| 3 | **TUI 审批弹窗**       | `cli/tui/app.py` + `approval.py`                     | ✅ FIXED         | `_make_approval_handler` 已替换 `approval_handler=None`（Commit `d967702`），inline 模式下弹窗接线完毕。                                                                                              |
| 4 | **TUI logo 渲染**      | `cli/tui/logo.py` + `app.py:_write_welcome`          | ✅ FIXED         | `render_logo()` 已在 welcome 卡片直接 mount；兜底为蓝色三角形吉祥物。                                                                                                                                            |
| 5 | **planner 精简提示词** | `prompts/planExecute.py` `PLANNER_PROMPT_SHORT`    | ✅ 已删除        | 无用定义随 Commit `08f5733` 删除，仅保留 `PLANNER_PROMPT`。                                                                                                                                                     |
| 6 | **formatter 死分支**   | `cli/formatter.py` + `cli/event_summary.py`        | ✅ FIXED         | `intent_decision` 渲染改读真实 payload 键；删除 `chat_response` 与 `actor`/`codeAgent`/`memory_snapshot` 死节点分支；新增 `peer_reply`/`human_request_recorded`/`workspace_note` 已发出事件处理（Commit `08f5733`）。 |
| 7 | **非数模默认计划**     | `graph/nodes.py:_default_plan` + 入口三路判定        | ⚠️ 已知分歧     | 入口判定实际是**三路**：`classify_user_input`（正则）→ coordinator（LLM）→ `_is_math_modeling_task`（关键词），结果可能不一致。**有意不改逻辑**，仅记录。                                                             |
| 8 | **论文模板资源重复**   | `src/assets/` + `header.tex / sections/*.tex`       | ✅ OBSOLETE      | header+sections 机制已移除；论文手仅用 `数模论文模板` seed 后就地填充 `论文.tex`。`src/assets/` 死副本随 Commit `2f55544` 删除。                                                                         |

> **测试判定**：以上 8 项均无专门测试；现有 58 项测试全部针对已接线路径，故测试通过**不能**证明这些模块有效（已接线的模块各有回归路径）。

---

## 8. 运行方式速查

```bash
uv sync --extra modeling --extra dev        # 安装依赖
uv run pytest                              # 58 项测试（无需 API key）
uv run python scripts/smoke_test.py        # 端到端冒烟（脚本化模型）
real-modelize --dry-run "测试题"            # 无 key 预览 UI
real-modelize --approval-mode auto -w <ws> "一道 CUMCM 题"   # 真实运行
real-modelize --resume <workspace>         # 从断点恢复
real-modelize compile -w <ws>              # 手动编译论文
real-modelize tui                          # Textual 界面
```

---

## 9. 产物与工作区结构

```
<workspace>/
├── 题目分析.md          建模手
├── 建模方案.json        建模手（EDA + 各问方案 + 敏感性）
├── problem{i}/          编程手（每问独立目录，硬性规则）
│   ├── 方案/ 代码/ 图表/ 结果/（含 evidence.json）
├── research/            研究手（研究资料.md + 参考文献.bib + 来源台账.json）
├── 论文.tex / 论文.pdf  论文手（数模论文模板 seed → 就地填充 + xelatex）
├── NOTEPAD.md           跨 Agent 持久笔记（图表清单+关键数字）
├── TODO.md / HISTORY_SUMMARY.md / SESSION_SUMMARY.md
└── .real-modelize/
    ├── checkpoints/  (checkpoint.json / RECOVERY.md / git 快照 / [strict] state.json, events.jsonl)
    ├── traces/trace-*/ (events.jsonl / summary.json / timeline.md)
    ├── session/session.json
    ├── bash-outputs/  (超长输出落盘)
    └── background/    (后台任务 out/err)
```

---

## 10. 建议的下一步（按优先级）

1. **统一数模判定**：`classify_user_input`(正则) → coordinator(LLM) → `_is_math_modeling_task`(关键词) 三路只保留一套，避免入口判定与 planner 默认计划不一致（§7.7）。
2. **补测试覆盖**：formatter/event_summary 新事件分支（peer_reply / human_request_recorded / workspace_note）与 TUI 审批接线目前无单测，可加轻量回归。
3. **算法资料库路径**：`RMA_ALGORITHM_ASSETS_DIR` 已在 `.env.example` 声明，部署时按需配置指向外部资产目录。
