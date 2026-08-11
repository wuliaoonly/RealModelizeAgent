# RealModelizeAgent

由 Planner 统筹的数学建模竞赛多智能体系统。系统按固定 Stage 顺序完成题目预处理、资料研究、建模、代码求解、绘图和 LaTeX 论文交付；每个 Stage 只有在 Verify 确认规定文件齐全后才能进入下一阶段。

## 工作流

```text
Coordinator
  └─ Planner
      ├─ Stage 0 Prepare  → Verify
      ├─ Stage 1 Analysis → Research Agent ↔ Model Agent → Verify
      ├─ Stage 2 Code     → Code Agent ↔ Model Agent     → Verify
      └─ Stage 3 Writing  → Write Agent ↔ Code Agent     → Verify → Complete
```

Verify 只检查产物是否存在且非空，不判断模型、代码结果或论文内容是否正确。内容质量由相应专业 Agent 的内部审查闭环负责。每个 Stage 的尝试次数有上限；失败时 Planner 仅把缺失文件清单交回当前 Stage，不会越级执行。

Analysis 和 Code Stage 通过门禁后，系统把上下文上限切换为 200K tokens，并将压缩摘要、重要路径和未完成事项持久化到 `NOTEPAD.md` / `HISTORY_SUMMARY.md`。

## Agent 职责

- Planner Agent：Stage 调度、TODO、NOTEPAD、循环中断、Agent handoff 和状态汇总。
- Research Agent：真实资料/论文检索，输出 `research/研究资料.md` 与 `research/refs.bib`；检索不可用时明确记录，不虚构来源。
- Model Agent：题目理解、模型与算法设计、公式/术语规范、代码方案一致性审查，可按问题并行拆分思路。
- Code Agent：分 `code` / `figure` 两种工作状态；负责求解代码、结果证据、敏感性分析和出版级图表。
- Write Agent：先写逐问 Markdown，再填充 `article/main.tex`，缺图时向 Code Agent 发出明确绘图请求，最后编译 PDF。
- Verify Agent：只读、确定性地核对当前 Stage 的必需文件。

## 安装

```powershell
uv sync --extra modeling --extra office --extra dev
```

复制 `.env.example` 为 `.env` 并配置模型。联网研究需要 `TAVILY_API_KEY`；没有该变量时系统会生成带明确“未检索”声明的研究文件，不会伪造文献。

## 使用

把题目 PDF、文本和 Excel/CSV 数据放入项目 `problem/`，然后运行：

```powershell
uv run real-modelize run "完成 problem/ 中的数学建模竞赛题"
```

指定工作区：

```powershell
uv run real-modelize run "完成该数学建模题" --workspace .real-modelize/workspaces/demo
```

恢复已有任务：

```powershell
uv run real-modelize run --resume .real-modelize/workspaces/demo
```

TUI 与无 API 演示：

```powershell
uv run real-modelize tui
uv run real-modelize run --dry-run
```

手动编译 canonical LaTeX 工程：

```powershell
uv run real-modelize compile --workspace .real-modelize/workspaces/demo --tex article/main.tex --engine xelatex
```

## 标准工作区

```text
workspaces/ProblemN/
├── TODO.md
├── NOTEPAD.md
├── raw/
│   ├── 题目.md
│   └── 原始附件.*
├── data/                         # 处理后的共享数据
├── research/
│   ├── 研究资料.md
│   └── refs.bib
├── 建模方案.md
├── 术语符号表.md
├── 建模结果.md
├── util/                         # 共享代码
├── tmp/                          # 临时/测试文件
├── problem1/
│   ├── 方案/{方案.md,模型公式.md}
│   ├── 代码/{Q1.py,README.md}
│   ├── 结果/Q1_evidence.json
│   ├── 图表/
│   └── 文稿.md
├── problem_sensitivity/
│   ├── 敏感性分析方案.md
│   ├── 代码/{sensitivity.py,README.md}
│   ├── 结果/sensitivity_evidence.json
│   └── 敏感性分析文稿.md
└── article/
    ├── main.tex
    ├── main.pdf
    └── 模板依赖资源
```

`StageCompletenessTool` 的完整规则位于 `core/stages.py`。Stage 0 会完整复制预设模板，但排除 `.aux/.log/.pdf` 等旧构建产物，保证新工作区从干净的 LaTeX 工程开始。

## 测试

```powershell
uv run pytest -q --basetemp=.test-tmp
```

当前测试覆盖 Stage 初始化与门禁、Agent handoff、上下文压缩、工具权限、LaTeX 编译记录、CLI/TUI 和无网络端到端假链路。

## 安全边界

- Model Agent 无 Bash；Research Agent 只有检索工具。
- Code Agent 写入限制在 `*/代码/`、`util/`、`utils/` 和 `tmp/`，正式结果/图表由受限执行的求解脚本生成。
- Write Agent 使用专用 LaTeX 编译工具，不获得通用 Shell。
- Verify Agent 只读；Stage 门禁不调用 LLM 判定内容正确性。
- 所有文件路径必须留在工作区，命令执行使用 allowlist 和 `shell=False`。

## 推荐技能（Claude Code Skills）

以下第三方 Claude Code 技能与本仓库工作流配合使用（人工写作/绘图/文献检索阶段），**不随仓库发布**——`skills/` 已加入 `.gitignore`，需要时单独安装到 Claude Code 的 skills 目录：

| 技能 | 用途 |
|---|---|
| `humanizer-zh` | 去除文本中的 AI 生成痕迹，让论文与回复读起来更自然。 |
| `nature-figure` | 按 Nature 系期刊标准创建/修订/审计科研图（Python matplotlib/seaborn 或 R ggplot2），含多面板、图件清单与期刊导出。 |
| `nature-academic-search` | 多来源学术检索（PubMed/CrossRef/arXiv/Scopus/ScienceDirect）与引文核对、参考文献管理。 |
| `academic-figure-prompt` | 为 AI 生图工具生成顶会风格学术配图提示词（框架图/架构图/流程图等）。 |

它们与仓库内建工具（`tools/paper_search`、`tools/figure_style` 等）互补：内建工具由 Agent 在工作区内自动调用；技能供你在编辑器里人工介入时使用。
