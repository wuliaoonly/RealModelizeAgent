# RealModelizeAgent 架构

## 1. 设计原则

系统以 Planner 为唯一全局调度中心，专业 Agent 只负责各自领域。工作流是显式的有限状态机，而不是由 LLM 自由决定是否跳转。状态顺序固定为：

```text
prepare → analysis → code → writing → complete
```

每个状态之后都进入同一个确定性 `stage_verifier`。门禁只验证规定路径是否存在且非空，不读取内容评判科学正确性。这样可将“流程完整性”和“专业质量判断”分开：前者可重复、可测试；后者留给 Model/Code/Write Agent 的反馈闭环。

## 2. LangGraph

```text
START
  │
  ▼
prepare_stage ──────────────┐
  │                         │ missing, below limit
  ▼                         │
stage_verifier ─────────────┘
  │ pass
  ▼
analysis_stage ─ Research ↔ Model
  │
  ▼
stage_verifier ─ fail → analysis_stage
  │ pass
  ▼
context_monitor (200K) → optional context_compressor
  │
  ▼
code_stage ─ Code ↔ Model review
  │
  ▼
stage_verifier ─ fail → code_stage
  │ pass
  ▼
context_monitor (200K) → optional context_compressor
  │
  ▼
writing_stage ─ Write ↔ Code(figure)
  │
  ▼
stage_verifier ─ fail → writing_stage
  │ pass
  ▼
final → END
```

相关实现：

- `graph/workflow.py`：图定义和边。
- `graph/stage_nodes.py`：Planner 的四个 Stage 节点、handoff 和门禁路由。
- `core/stages.py`：Stage enum、产物合同、纯确定性 Verify。
- `core/preparation.py`：Stage 0 的输入迁移、题目提取、目录初始化和模板复制。
- `graph/state.py`：跨节点状态合同。

## 3. 状态与信息流

`RealModelizeGraphState` 的 Stage 核心字段：

| 字段 | 含义 |
|---|---|
| `stage` | 当前 Stage |
| `stage_attempts` | 每个 Stage 独立的尝试计数 |
| `stage_verifications` | 每阶段最近一次完整性检查 |
| `stage_history` | 不可变式的门禁历史摘要 |
| `stage_next_node` | Planner 决定的下一节点 |
| `context_token_limit` | Analysis/Code 边界固定为 200K |

Agent handoff 的摘要、来源、图表、结果、论文路径和 TODO 会回写 graph state；关键跨阶段决策同时追加到 `NOTEPAD.md`。完整上下文压缩产物写入 `HISTORY_SUMMARY.md`，使 checkpoint/resume 不依赖内存中的长消息历史。

## 4. Stage 合同

### Stage 0 Prepare

输入为项目 `problem/` 和 `数模论文模板/`。输出至少包括 `raw/题目.md`、`article/main.tex`、`TODO.md`、`NOTEPAD.md`。PDF 用 `pypdf` 抽取；文本附件直接合并；Excel 等原始附件原样复制到 `raw/`。

### Stage 1 Analysis

Research 输出 `research/研究资料.md`、`research/refs.bib`。Model 输出总 `建模方案.md`、`术语符号表.md`、每问 `方案.md`/`模型公式.md` 和敏感性分析方案。Model 可在内部调用 Research 定向补充。

### Stage 2 Code

Code 为每问及敏感性分析生成独立代码、README 和 `*_evidence.json`。Model 审查方案一致性与结果合理性，可要求 Code 迭代，固化后生成 `建模结果.md`。Verify 不重复这些专业判断。

### Stage 3 Writing

Write 先生成逐问 Markdown。缺图时用明确的 `figure` 状态请求 Code Agent；随后就地填充 `article/main.tex`，使用专用工具从 `article/` 目录执行 XeLaTeX 两遍，生成 `article/main.pdf`。

## 5. Agent 与权限

| Agent | 写权限 | 工具要点 |
|---|---|---|
| Planner | 整个工作区 | Stage 调度、TODO、NOTEPAD、handoff |
| Research | `research/` 语义范围 | WebSearch/PaperSearch，无 Shell |
| Model | 工作区方案文档 | FileRead/Write、Grep、Notepad、XlsxRead，无 Bash |
| Code | `*/代码/`、`util(s)/`、`tmp/` | 受限 Bash、数据与绘图工具 |
| Write | 工作区论文与文稿 | File tools、专用 LaTeX、PDF/DOCX，无通用 Shell |
| Verify | 只读 | StageCompleteness、FileRead、Grep、NotepadRead |

运行时路径由 `RuntimeState` 约束：写路径必须在 workspace 内；读路径限 workspace 或项目只读根。Code 的结果和图表由受限执行脚本生成，执行记录与源码哈希可供专业 Agent 审计。

## 6. LaTeX 工程

canonical 主文件是 `article/main.tex`。`compile_latex` 支持工作区内的嵌套主文件，使用主文件所在目录作为 `cwd`，并对同目录下 `.tex/.bib` 及引用图片计算源码指纹。旧工作区根 `论文.tex` 仍由兼容 API 识别，但新任务只生成 `article/` 工程。

## 7. 恢复、追踪与终止

Checkpoint 保存 Stage、尝试次数、产物索引和摘要。恢复时从保存的 `stage` 继续，而不是重新执行已通过的 Stage。每阶段达到 `max_attempts` 仍缺文件时进入 `final` 并返回 FAILED，避免无限循环。Trace 记录 handoff、工具调用、门禁结果和上下文压缩事件。

## 8. 测试边界

测试分为纯函数 Stage 合同、工具安全、Agent handoff、Graph 路由、上下文压缩、LaTeX 状态和脚本化 E2E。E2E 用假 Agent 生成完整的新目录合同，无需 API key 或联网。
