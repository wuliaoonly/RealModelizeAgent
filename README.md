# RealModelizeAgent · 数学建模 Code Agent

一个用 LangGraph + LangChain + Typer 构建的**数学建模 Code Agent**（Claude Code 的 mini 版）。
输入一道数模竞赛题（CUMCM/MCM），端到端输出：

- `题目分析.md`、`建模方案.json` —— 问题重述、模型选择与结构化方案总纲（建模手）
- `problem1/`、`problem2/`… —— **每问独立目录**：方案、独立可执行代码、图表、结果与 `evidence.json`
- `research/研究资料.md` —— 研究手真实联网检索结果（仅真实返回，禁止编造）
- `research/参考文献.bib` —— 研究手为每条真实来源生成的 BibTeX 条目（键名 `rN`，供论文手 `\cite` 引用）
- **`论文.tex` / `论文.pdf`** —— CUMCM 风格论文（专用 LaTeX 工具编译并记录源码指纹、退出码和日志状态）

工作流：`coordinator（判定数模题）→ planner（规划+全链路监督）→ 研究手/建模手 → 编程手 → 论文手 → verifier（验收循环）`，
Agent 间自带反馈闭环：**研究手↔建模手**（资料不足再检索）、**建模手↔编程手**（结果核验不达标下发修订）、
**论文手↔编程手**（图表轮逐问把关修图）；论文手写作轮面向全文（摘要/问题背景/问题重述/建模思路/各问求解/建模优缺点），
按需向其他 Agent 索要素材。

## 安装

```bash
uv sync --extra modeling --extra dev   # modeling = 编程手运行所需科学计算库
```

## 配置

复制 `.env.example` 为 `.env` 并填写：

```bash
API_KEY=sk-...            # OpenAI 兼容 key（官方 / DeepSeek / 智谱 / vLLM 等）
MODEL=gpt-4o
BASE_URL=https://api.openai.com/v1
TAVILY_API_KEY=           # 可选，研究手联网检索
```

Agent 行为参数用 `RMA_*` 前缀（见 `.env.example`，均有默认值），其中各 Agent 反馈闭环轮次上限：
`RMA_MODELER_MAX_LOOPS=24`、`RMA_WRITER_MAX_LOOPS=28`、`RMA_CODER_MAX_LOOPS=14`、
`RMA_RESEARCH_MAX_LOOPS=6`、`RMA_PLANNER_MAX_LOOPS=12`、`RMA_VERIFIER_MAX_LOOPS=10`。

## 使用

```bash
# 一次性 Rich CLI（推荐）
real-modelize "某市 2024 年燃气用量的预测建模，给出不同区域未来半年的用量预测"

# 无 API key 预览 UI（内置脚本化事件流）
real-modelize --dry-run "测试题目"

# Textual TUI（含审批弹窗 + logo）
real-modelize tui

# 对已有工作区手动编译论文
real-modelize compile --workspace path/to/workspace
```

常用标志：`--workspace/-w`、`--max-attempts`（默认 3）、`--approval-mode inline|auto|deny`、
`--checkpoint-mode light|strict|off`、`--trace-mode on|off`、`--resume <workspace>`。

### 人工反馈与定向修改

入口会先把输入识别为 `chat`、`instruction` 或新 `task`。闲聊只汇总多个专家 Agent 的只读回复；
已有工作区的指令交给 planner，写入现有计划或 `execution_commands` 后再委派，不会被 coordinator 当作非数模题拒绝。

```powershell
# 修改已有工作区的全部图表：字号、中文字体回退和整体配色会写入统一样式配置并自动质检
real-modelize --resume path/to/workspace "把图表字号设为 14，改成暖色配色并重新出图"

# 只修改论文中的一个段落；writer 用章节 + 段落序号/唯一锚点精确定位，再重新编译
real-modelize --resume path/to/workspace "修改论文中模型的评价和改进章节第 2 段，使表述更严谨"
```

人工请求记录在 `.real-modelize/human-loop/requests.jsonl`，图表样式记录在
`.real-modelize/human-loop/figure-style.json`。coder 的 `FigureAuditTool` 会检查中文字体配置、用户配色和 PNG 清晰度；
writer 的 `PaperParagraphEditTool` 在目标不唯一时拒绝修改，避免误改整篇论文。

## 工作区结构

一次任务的工作区（默认 `.real-modelize/workspaces/workspace-*`）：

```
题目分析.md 建模方案.json 论文.tex 论文.pdf Pictures/（模板 seed）
research/研究资料.md       # 研究手真实联网检索结果（禁止编造）
research/参考文献.bib     # 研究手真实检索生成的 BibTeX（键名 rN，论文手转行内 thebibliography）
problem1/                  # 每问独立目录（数量 = ques_count）
  ├── 方案/问题1_方案.md    # 建模手：该问建模方案
  ├── 代码/问题1_求解.py    # 编程手：该问求解脚本
  ├── 图表/fig1_*.png       # 编程手：该问核心图（EDA/敏感性图在 problem1/图表/EDA|Sensitivity/）
  └── 结果/                 # 结果数据 + evidence.json + execution.json
problem2/ ...              # 依题目问数扩展
NOTEPAD.md                # 跨 Agent 持久化笔记（图表清单 + 关键数字）
TODO.md                   # 任务清单
.real-modelize/           # checkpoint / trace / session
```

## 测试（无需 API key）

```bash
uv run pytest                          # 全部测试（FakeModel 假返回，不联网）
uv run pytest -m compile               # 真实 xelatex 编译国赛模板探针 + pdflatex 兼容探针（本机需 TeX Live）
uv run python scripts/smoke_test.py    # 端到端冒烟：脚本化模型假跑完整链路，打印 PASS/FAIL
```

真实运行：配置好 `.env`（API_KEY/MODEL/BASE_URL）后，

```bash
real-modelize --approval-mode auto -w /tmp/my_ws "一道CUMCM题……"
```

产物在工作区 `/tmp/my_ws/` 下：`题目分析.md`、`建模方案.json`、`research/研究资料.md`、
每问 `problem1..N/{方案,代码,图表,结果}`、`论文.tex`、`论文.pdf`。

## 架构

```
src/real_modelize_agent/
├── agents/       建模手 / 编程手 / 论文手 / 研究手（ReAct 循环，经 planner 工具移交）
├── cli/          typer 入口、rich 格式化、事件摘要、基础 textual TUI
├── core/         agent 编排流、审批、checkpoint、session、状态、路径、trace
├── graph/        LangGraph 状态图（coordinator→planner→verifier 循环）与分层记忆
├── prompts/      建模/绘图/写作/规划/多Agent/上下文压缩 六组提示词
├── providers/    OpenAI 兼容模型工厂
└── tools/        文件、shell、grep、便签、任务、网络搜索等工具注册
```

## 可信验收与证据链

- verifier 的最终结论必须同时通过运行时确定性门禁和 LLM 语义质检；LLM 不能省略强制检查。
- verifier 与论文手没有通用 Shell。编程手命令执行采用 allowlist、`shell=False`，拒绝管道、重定向、后台任务与 `python -c/-m`。
- 每问入口脚本必须可独立运行。受限执行器自动写入 `problemN/结果/execution.json`，记录源码 SHA256 与退出码。
- 每问 `evidence.json` 记录输入哈希、模型假设/单位、验证切分、泄漏控制、基线、诊断、敏感性范围依据、Figure Contract 与论文 claim。
- `论文.pdf` 存在不代表编译成功；只有专用编译记录成功、无致命日志错误且源码指纹未变化时才算通过。
- checkpoint Git 排除环境文件、密钥和大型二进制产物，并按节点阶段去重快照。
