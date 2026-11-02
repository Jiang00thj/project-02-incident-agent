"""操作手册：从零到能面试讲透，每一步都有命令和预期输出。对不上就停下排查。"""

# STEPS · project-02-incident-agent

约定：`python` = `D:\python\python.exe`（3.10+ 皆可，核心零依赖）；
项目一在 `../project-01-mcp-gateway`（不在就设 `PROJECT01_ROOT` 环境变量）。

## 阶段 0 · 环境（10 分钟）

1. `python -c "import sys; print(sys.version)"` → 3.10+
2. `python -m unittest discover -s tests -v 2>&1 | tail -3`
   → `Ran 39 tests in ~3s  OK`（含真网关集成测试；若项目一缺失会自动 skip）
3. `python demo.py` → 7 个场景依次打印，末尾 `全部断言通过。`

对不上就别往下走：先看 `gateway_audit.jsonl` 是否生成（网关是否启动）。

## 阶段 1 · 读懂 minigraph（1 天，最值的一天）

4. 通读 `minigraph/graph.py` 的 `invoke()`：找到超步循环、边界合并、
   路由、环检测四段。能回答"为什么合并在边界而不是节点内"。
5. `python -m unittest tests.test_graph -v` → 12 个全绿。挑 2 个断言改坏
   （如把 InvalidUpdateError 换成静默覆盖），看哪些测试红——这是你的回归网。
6. 白板复现：不看代码画出 `intake→[scan_logs∥check_known]→diagnose→
   await_approval→apply_fix→verify→report` 的超步序列（几个超步？何时合并？）。
   预期：intake 1 步、并行探针 1 步、diagnose 1 步、await_approval 中断……

## 阶段 2 · 跑通对照实验（半天）

7. `python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt`
8. `.venv/Scripts/python adapters/langgraph_adapter.py`
   → 两引擎 `status/interrupts/findings` 一致，`semantics agree: True`
9. 打开 `adapters/langgraph_adapter.py` 对比两种 API 写同一张图的差异
   （`Command(goto=)` vs 条件边、`interrupt()` 的 resume 通道形状）。

## 阶段 3 · 真模型（1 天，出简历数字）

10. `set BENCH_API_KEY=<你的 DeepSeek key>`（PowerShell）或 `export`（Git Bash）。
    **key 永远不进文件、不进命令历史以外的任何地方。**
11. `python bench.py --model deepseek --json results_deepseek.json`
    → 把 summary 抄进 README 的表格（scripted 行保留，注明是管道自检）。
12. 跑 3 次取中位数；记录模型答错的节点（日志贴到
    `scenario/incidents.py` 的 expected 旁边，作为"真模型失败模式"素材）。

## 阶段 4 · 自己动手改（1-2 天，面试差异化）

13. 把 `MAX_ITERATIONS` 从 2 改成 3，重跑 INC-2403，观察成本曲线。
14. 给 `await_approval` 加"审批人不在→超时自动转 escalate"（新 interrupt 载荷
    + 检查点），这是你自己的增量，README 的 Difference 表加一行。
15. 故意把 `logs.restart_service` 的 risk 改成 read（注释掉风险表那一行），
    跑 demo 场景 3 → 应看到 fail-closed：节点拒绝执行未设门的写操作。

## 阶段 5 · 发布

16. 分批 commit（本仓库初始已按模块分好批次，别一次 push 完）。
17. README 的量化表、已知边界更新；截图 demo 场景 3/6 放 `docs/`。
18. 面试自测：录音口述五道必答题各 90 秒；40 秒演示 = demo 场景 3 + 6。

## 时间不够砍什么

- 砍阶段 2（对照实验）保阶段 1（手写内核）——后者是项目灵魂。
- 砍阶段 4 的第 14 条保 13/15——超时自动升级是大改，两行改动的实验更稳。
- 永远不砍：阶段 3（没有真数字，量化叙事就是空的）。
