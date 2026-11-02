"""End-to-end walkthrough of the incident-triage agent.

    python demo.py

Seven scenes, every one asserted (a broken policy fails loudly):

  1. discovery   -- the real project-01 gateway aggregating 3 servers / 8 tools
  2. fan-out     -- two sub-agents in ONE superstep, findings merged by reducer
  3. approval    -- the gateway's gate *driving* the graph's interrupt
  4. denial      -- human says no: escalate instead of restart (postmortem still published)
  5. reflection  -- wrong fix -> loop -> iteration budget -> escalate; idempotent restart
  6. crash+resume-- process dies after the human's yes; replay restarts nothing twice
  7. audit       -- what the gateway logged about all of it
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8")

from minigraph import MemoryCheckpointer, SqliteCheckpointer  # noqa: E402
from model import ScriptedModel  # noqa: E402
from tools.gateway_tools import GatewayTools, FakeGatewayTools  # noqa: E402
from scenario.graph import build_graph  # noqa: E402
from scenario.state import INITIAL  # noqa: E402
from scenario.incidents import INCIDENTS, incident_by_id  # noqa: E402

AUDIT_PATH = os.path.join(ROOT, "gateway_audit.jsonl")


def title(text):
    print(f"\n{'=' * 64}\n{text}\n{'=' * 64}")


def run_until_done(app, thread_id, incident, approver):
    """Drive the graph, answering every interrupt with `approver(payload)`."""
    state = dict(INITIAL)
    state["incident"] = incident
    out = app.invoke(state, {"thread_id": thread_id})
    interrupts = []
    while "__interrupt__" in out:
        payload = out["__interrupt__"]["payload"]
        interrupts.append(payload)
        out = app.invoke(None, {"thread_id": thread_id},
                         resume=approver(payload))
        if len(interrupts) > 6:
            raise AssertionError("too many interrupts")
    return out, interrupts


def main():
    if os.path.exists(AUDIT_PATH):
        os.remove(AUDIT_PATH)

    # ---------------------------------------------------------------- scene 1
    title("场景 1 · 工具层：项目一网关聚合 3 个 MCP Server / 8 个工具")
    real_tools = GatewayTools.build(audit_path=AUDIT_PATH)
    tools_by_server = {}
    for tool in real_tools.list_tools():
        tools_by_server.setdefault(tool["name"].split(".")[0], []).append(tool["name"])
    for server, names in sorted(tools_by_server.items()):
        print(f"  {server:<8} {len(names)} 个工具: {', '.join(sorted(names))}")
    assert len(tools_by_server) == 3 and sum(map(len, tools_by_server.values())) == 8

    # ---------------------------------------------------------------- scene 2+3
    title("场景 2/3 · 并行子代理 + 网关审批门驱动的中断（INC-2401, 真网关）")
    inc = incident_by_id("INC-2401")
    model = ScriptedModel(dict(inc["script"]))
    app = build_graph(model, real_tools, checkpointer=MemoryCheckpointer())
    state = dict(INITIAL)
    state["incident"] = inc["incident"]
    out = app.invoke(state, {"thread_id": "demo-2401"})
    gate = out["__interrupt__"]
    print("  图在写操作前停下，中断负载直接来自网关的 ApprovalRequired：")
    print(f"    tool={gate['payload']['tool']}  risk={gate['payload']['risk']}")
    print(f"    args={gate['payload']['args_preview']}")
    assert gate["payload"]["tool"] == "logs.restart_service"
    assert gate["payload"]["risk"] == "admin"
    # 同一超步里两个子代理并行执行，findings 各自追加、成本各自累加
    probe = [m for m in out["messages"]]
    assert any("scan_logs" in m for m in probe) and any("check_known" in m for m in probe)
    print("  并行证据已合并：", [f["source"] for f in out["findings"]])

    print("\n  [人工] 批准重启 ->")
    out = app.invoke(None, {"thread_id": "demo-2401"}, resume=True)
    postmortem = out["__interrupt__"]
    print(f"  第二个审批点（复盘发布）：{postmortem['payload']['question']}")
    print("  [人工] 批准发布 ->")
    out = app.invoke(None, {"thread_id": "demo-2401"}, resume=True)
    print(f"  结果：status={out['status']} 根因={out['outcome']['root_cause'][:36]}…")
    assert out["status"] == "fixed" and out["outcome"]["postmortem_published"] is True
    real_tools.stop()

    # ---------------------------------------------------------------- scene 4
    title("场景 4 · 拒绝路径：人工说停，Agent 转升级而不是硬来")
    tools = FakeGatewayTools()
    inc = incident_by_id("INC-2401")
    model = ScriptedModel(dict(inc["script"]))
    app = build_graph(model, tools, checkpointer=MemoryCheckpointer())
    out, interrupts = run_until_done(
        app, "demo-deny", inc["incident"],
        approver=lambda payload: "restart" not in payload["tool"])
    print(f"  中断数={len(interrupts)}（重启被拒，复盘被批准）")
    print(f"  status={out['status']}（denied -> escalate），复盘已发布="
          f"{out['outcome']['postmortem_published']}")
    assert out["status"] == "denied" and out["outcome"]["postmortem_published"] is True
    assert out["cost"]["denials"] == 1

    # ---------------------------------------------------------------- scene 5
    title("场景 5 · 反思循环：错误处置 -> 复查 -> 迭代预算 -> 升级（INC-2403）")
    tools = FakeGatewayTools()
    inc = INCIDENTS[2]
    model = ScriptedModel(dict(inc["script"]))
    app = build_graph(model, tools, checkpointer=MemoryCheckpointer())
    out, interrupts = run_until_done(app, "demo-loop", inc["incident"],
                                     approver=lambda payload: True)
    for message in out["messages"]:
        print("   ", message)
    print(f"  中断数={len(interrupts)}（两次重启 + 一次复盘）"
          f" 迭代={out['iteration']} status={out['status']}")
    assert out["status"] == "escalated" and out["iteration"] == 2
    assert any("already_restarted=True" in m for m in out["messages"]), \
        "第二次重启必须命中幂等保护"

    # ---------------------------------------------------------------- scene 6
    title("场景 6 · 崩溃恢复：批准后进程死亡，重放不重复执行写操作")
    db_path = os.path.join(ROOT, "demo_checkpoints.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    tools = FakeGatewayTools()
    inc = incident_by_id("INC-2401")
    model = ScriptedModel(dict(inc["script"]))
    app = build_graph(model, tools, checkpointer=SqliteCheckpointer(db_path))
    state = dict(INITIAL)
    state["incident"] = inc["incident"]
    out = app.invoke(state, {"thread_id": "demo-crash"})
    print("  第一次中断（重启审批）已到达。")
    # 模拟：人工批准后、节点把结果写回状态前，进程死亡
    nodes = app._nodes
    original = nodes["await_approval"].fn
    dead = {"once": True}

    def die_after_approval(snapshot):
        result = original(snapshot)
        if dead["once"]:
            dead["once"] = False
            raise RuntimeError("simulated process death after approval")
        return result

    nodes["await_approval"].fn = die_after_approval
    try:
        app.invoke(None, {"thread_id": "demo-crash"}, resume=True)
    except RuntimeError as exc:
        print(f"  崩溃注入：{exc}")
    nodes["await_approval"].fn = original

    # “图进程重启”：全新图实例 + 同一检查点文件；网关与 server 子进程存活，
    # 所以工具侧的幂等保护（already_restarted）是真正被考验的那一层。
    model2 = ScriptedModel(dict(incident_by_id("INC-2401")["script"]))
    app2 = build_graph(model2, tools, checkpointer=SqliteCheckpointer(db_path))
    out = app2.invoke(None, {"thread_id": "demo-crash"}, resume=True)
    out = app2.invoke(None, {"thread_id": "demo-crash"}, resume=True)
    replay = [m for m in out["messages"] if "already_restarted" in m]
    print(f"  恢复完成：status={out['status']}")
    print(f"  重放时的幂等保护：{replay[-1] if replay else '未触发重启'}")
    probe = SqliteCheckpointer(db_path)
    steps = len(probe.list_steps("demo-crash"))
    probe.close()
    app.checkpointer.close()
    app2.checkpointer.close()
    print(f"  检查点轨迹：{steps} 个超步边界（SqliteCheckpointer）")
    print("  （若连 server 状态也丢了，幂等就必须落在服务端控制面——重放边界要分清）")
    assert out["status"] == "fixed"
    assert replay and "already_restarted=True" in replay[-1]
    os.remove(db_path)

    # ---------------------------------------------------------------- scene 7
    title("场景 7 · 审计日志：网关视角的完整调用轨迹")
    with open(AUDIT_PATH, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    from collections import Counter
    for decision, count in Counter(r["decision"] for r in records).most_common():
        print(f"  {decision:<16} {count}")
    print(f"  参数只存 SHA256 摘要（隐私），日志共 {len(records)} 条"
          f" -> {os.path.basename(AUDIT_PATH)}")
    assert any(r["decision"] == "needs_approval" for r in records)

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
