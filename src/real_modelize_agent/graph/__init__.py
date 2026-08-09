"""RealModelizeAgent 图编排：coordinator 判定 + 数模多 Agent 工作流。

注意：本包不在此处重导出 nodes/workflow 符号（会触发
agents → graph.memory → graph 包 → nodes → agents 的循环导入）。
统一从子模块导入：``real_modelize_agent.graph.nodes`` / ``.workflow`` / ``.memory``。
"""
