# project-02 · incident-agent：多 Agent 排障编排层

> 手写编排内核（minigraph）+ LangGraph 语义对照 + 复用项目一 MCP 网关作工具层。
> 垂直场景：微服务事件排障（Supervisor 分派 → 并行子代理 → 反思循环 → 人工审批中断 → 检查点续跑）。

[![CI](https://github.com/Jiang00thj/project-02-incident-agent/actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)

**这是四个递进项目的第二层。** 项目一（[project-01-mcp-gateway](https://github.com/Jiang00thj/project-01-mcp-gateway)）解决“Agent 用什么工具”，本项目解决“多 Agent 怎么被编排起来”——并把项目一的网关原样接进来当工具层：权限、审批门、审计、预算全部继承，一点没有重写。

## 为什么先手写再对比

调包者答不出“LangGraph 的调度模型是什么”。所以本项目先零依赖手写 minigraph（~300 行核心），再把**同一个排障场景**在真 LangGraph 1.2 上复刻一遍（`adapters/langgraph_adapter.py`），断言两个引擎产出一致：

```
== minigraph ==
  status=fixed interrupts=2 findings=['github', 'logs'] messages=8
== langgraph 1.2 ==
  status=fixed interrupts=2 findings=2 messages=6
semantics agree: True
```

会手写，也真的会用，还能说出两边的差别——这才是编排层项目该有的深度。

## 架构

```
                ┌──────────────────────────────────────────────┐
                │  minigraph（手写编排内核，零依赖）             │
                │  超步循环 / reducer 状态合并 / 检查点 / 中断   │
                └───────────────┬──────────────────────────────┘
                                │ 节点 = 闭包(model, tools)
        ┌───────────────────────┼───────────────────────────┐
        ▼                       ▼                           ▼
  ScriptedModel          DeepSeekModel                GatewayTools
  （离线可测）          （OpenAI 兼容，key 只走环境变量）  （项目一网关，进程内复用）
                                                        │
                                    ┌───────────────────┼──────────────────┐
                                    ▼                   ▼                  ▼
                              logs-server(新)     github-server      notice-server
                              查日志/状态/重启     已知问题检索        复盘发布
                              （MCP, 项目一协议层 mcp_min）
```

图的形状（`scenario/graph.py`）：

```
START → intake ──(并行扇出)──→ [scan_logs ∥ check_known] ──(扇入)──→ diagnose
diagnose ──(fix_action)──→ await_approval（网关审批门→interrupt）| escalate
await_approval ──(人工)──→ apply_fix → verify ──(healthy? iteration?)──→ report | scan_logs | escalate
report ──(复盘发布审批)──→ END
```

## 快速开始

```bash
python demo.py          # 7 个场景，全部断言（含真网关端到端）
python -m unittest discover -s tests    # 39 个单测（含 LangGraph 适配外的全部）
python bench.py         # 五项量化指标（scripted 版，离线确定）
python bench.py --model deepseek   # 真模型，需 BENCH_API_KEY 环境变量

# LangGraph 对照实验（可选，需 venv）：
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python adapters/langgraph_adapter.py
```

依赖：项目一在同级目录 `../project-01-mcp-gateway`（或设 `PROJECT01_ROOT`）。核心代码零第三方依赖。

## 量化指标（bench.py）

| 指标 | scripted 基线 | 说明 |
|---|---|---|
| 任务完成率 | 100%（3/3） | 终态与事件集预期一致 |
| 平均超步数 | 10.3 | 检查点边界计数（编排长度） |
| 模型调用 / tokens | 5.7 次 / 4703 tokens 每任务 | 含并行子代理各一次 |
| 工具调用 | 6.3 次每任务 | 全部经项目一网关（含审批拒绝的尝试） |
| 人工介入率 | 100%（中断 2.0 次每任务） | 重启审批 + 复盘发布审批 |
| 时延 | P50 3.5ms / P95 4.4ms | 本地 scripted；真模型看 `--model deepseek` |

> **scripted 数字只证明管道通，不能进简历。** 真实完成率/成本/时延必须用 `--model deepseek` 跑出来（参考项目一：真模型数字 + 实验设计才是量化叙事）。

## 五个设计决策（面试提词板）

1. **审批门在网关，中断在图。** `await_approval` 故意不带 `approved=True` 调用网关，让网关的 `ApprovalRequired` 异常**驱动**图的中断——策略只有一处定义，图只是把决策递给人类。若网关风险表配错没拦住写操作，节点会拒绝执行并转升级（fail closed）。
2. **中断停在被中断节点的写入合并之前。** 重放 = 整节点重新执行，不是从断点续跑半截函数。代价是节点必须幂等——`logs.restart_service` 返回 `already_restarted=True` 而不是重启两次。demo 场景 6 专门演示“人工批准后进程崩溃→重放不重复执行写操作”。
3. **并行扇出必须配 reducer。** 两个子代理同一超步各写 `messages/findings`（`Annotated[list, add]`）和 `cost`（自定义按 key 求和的 reducer）。last-value 字段被两个节点同超步写入时直接抛 `InvalidUpdateError`——静默覆盖才是最贵的 bug。
4. **两道循环闸门。** 步数预算（`recursion_limit`，LangGraph 同款）之外加了**状态环检测**：同一 `(状态摘要, 活跃节点)` 签名重复 3 次即终止并给出诊断。步数预算抓不到“每步都在跑但状态不再变化”的死循环——那正是烧钱最凶的形态。
5. **错误兜底走恢复节点。** 节点级 `retry/timeout/error_handler` + `set_node_defaults`（对齐 LangGraph 1.2 的新 API 形状）：重试预算耗尽后跑一次恢复节点，拿到 `{node, error}` 上下文，返回 Command 改路由。恢复节点只执行一次（实现时踩过执行两次的坑，测试锁死）。

## Difference from upstream

本项目**不是** LangGraph 的 fork——是“手写等价内核 + 官方实现对照”：

| 能力 | minigraph | LangGraph 1.2 |
|---|---|---|
| 超步调度 / reducer 合并 / 条件路由 | ✅ | ✅ |
| interrupt → 人工审批 → resume | ✅ | ✅（`interrupt()`/`Command(resume=)`） |
| 检查点 | SQLite（全量快照/每边界一行） | SQLite/Postgres，1.2 起 DeltaChannel 增量压缩 |
| 节点策略 retry/timeout/error_handler | ✅（语义对齐 1.2） | ✅ 1.2 新增 |
| 状态环检测 | ✅（自研，本项目增量） | ❌ 只有步数预算 |
| 并行超步 | ✅（线程池） | ✅（Pregel 通道语义，更完整） |
| 流式输出 / 分布式 / OpenTelemetry | ❌ 不做（刻意，见下） | ✅ |

不做 DeltaChannel/流式/分布式的理由写在这里：本项目的目标是**把调度循环讲到能白板复现**，这三个方向各自都是完整工程课题，塞进来只会稀释“读懂核心”这条主线。检查点存全量的代价在 README 数据规模（每任务 ≤20 边界）下不构成问题。

## 面试必答（自己先写过一遍再背）

1. **Pregel/BSP 调度模型**：节点在超步内执行，写入在边界处合并，路由决定下一个活跃集——为什么必须在边界合并（并行写冲突要在稳定点裁决）。
2. **检查点如何做状态快照**：边界时刻状态 + 下一活跃集 + 待决中断，三者一起落库；恢复即重放。
3. **interrupt 恢复的幂等**：中断停在写入合并前 → 整节点重放 → 工具必须幂等或可补偿；幂等放工具侧（服务端），因为图进程死了不能替服务端记住“已经做过”。
4. **怎么评估一条轨迹好不好**：本项目用五项（完成率/超步数/成本/介入率/时延）+ scripted/真模型双跑——单一指标会把“模型合理追问参数”误判成失败（项目一的教训）。
5. **为什么 approval 放网关不放图里**：权限/审计/审批是一套策略，图只是执行者；两处定义迟早漂移。

## 与项目一的关系

- **工具层零重写**：`tools/gateway_tools.py` 进程内导入项目一 `Gateway`，三个 MCP server（新写的 `logs` + 复用的 `github`/`notice`）由网关子进程拉起，走同一条 stdio JSON-RPC 协议（`mcp_min`，2026-07-28 无状态版）。
- **新数据面**：`mcpservers/logs_server.py` 是合成微服务舰队（auth 内存泄漏 / course-api 连接池回归 / gateway 健康），确定性、可复现。
- 后续项目三（mini-swe-agent 执行层）与项目四（组装 + 评测）继续复用这两层。

## 仓库结构

```
minigraph/        手写编排内核：graph.py（超步循环）/ types.py / checkpoint.py
adapters/         langgraph_adapter.py：同场景双引擎对照（可选依赖）
scenario/         排障场景：state.py / nodes.py / graph.py / incidents.py（3 个事件）
mcpservers/       logs_server.py：新的 MCP 观测面 server（复用项目一协议层）
tools/            gateway_tools.py：项目一网关接入 + 测试用 FakeGateway
model/            ScriptedModel（离线确定）/ DeepSeekModel（真模型，key 只走环境变量）
tests/            39 个单测：图语义 / 中断恢复 / 节点策略 / 场景 / 真网关集成
demo.py           7 场景演示（含崩溃恢复与幂等重放）
bench.py          五项量化指标（--model deepseek 出真数字）
```

## 已知边界与下一步

- [ ] 真模型数字：`bench.py --model deepseek`（BENCH_API_KEY）跑 3 事件 × 多次，填上面的表。
- [ ] 检查点存储只做全量快照；DeltaChannel 式增量压缩留作对照讨论点。
- [ ] `get_state` 只有最新检查点；时间旅行（读历史边界）没做。
- [ ] 拒绝路径 (`--deny`) 的完成率语义要按“安全优先”单独解释，当前 expected_status 只定义了配合人工的正常路径。
