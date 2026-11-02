"""Benchmarks for the incident-triage agent.

    python bench.py                     # offline, scripted model (deterministic)
    python bench.py --json out.json     # also dump raw numbers
    python bench.py --model deepseek    # real model, needs BENCH_API_KEY in env

Metrics (the five the project README promises):
  completion rate  -- terminal status matches the expected outcome per incident
  avg supersteps   -- orchestration length (checkpoint boundaries)
  cost per task    -- model calls / tokens / tool calls / approvals
  human touch rate -- share of tasks that needed at least one approval interrupt
  latency          -- wall clock P50 / P95 per task

The scripted numbers are for wiring verification only: they say the
plumbing works, not that the agent is smart. Real numbers need
--model deepseek (see README: which parts are ours, which are the model's).
"""

import argparse
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from minigraph import MemoryCheckpointer  # noqa: E402
from model import ScriptedModel  # noqa: E402
from tools.gateway_tools import FakeGatewayTools  # noqa: E402
from scenario.graph import build_graph  # noqa: E402
from scenario.state import INITIAL  # noqa: E402
from scenario.incidents import INCIDENTS  # noqa: E402


class CountingCheckpointer(MemoryCheckpointer):
    """Memory saver that also counts superstep boundaries."""

    def __init__(self):
        super().__init__()
        self.puts = 0

    def put(self, checkpoint):
        self.puts += 1
        super().put(checkpoint)


def run_case(incident, approver, model_factory):
    tools = FakeGatewayTools()
    model = model_factory(incident)
    checkpointer = CountingCheckpointer()
    app = build_graph(model, tools, checkpointer=checkpointer)
    state = dict(INITIAL)
    state["incident"] = incident["incident"]
    started = time.perf_counter()
    interrupts = 0
    out = app.invoke(state, {"thread_id": incident["id"]})
    while "__interrupt__" in out:
        interrupts += 1
        out = app.invoke(None, {"thread_id": incident["id"]},
                         resume=approver(out["__interrupt__"]["payload"]))
        if interrupts > 8:
            raise AssertionError("interrupt loop")
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "incident": incident["id"],
        "expected": incident["expected_status"],
        "status": out["status"],
        "supersteps": checkpointer.puts,
        "interrupts": interrupts,
        "latency_ms": round(elapsed_ms, 1),
        "cost": out["cost"],
        "messages": out["messages"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=("scripted", "deepseek"), default="scripted")
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--deny", action="store_true",
                        help="human denies every write (drives escalation paths)")
    args = parser.parse_args()

    if args.model == "deepseek":
        from model import DeepSeekModel
        model_factory = lambda inc: DeepSeekModel()  # noqa: E731
    else:
        model_factory = lambda inc: ScriptedModel(dict(inc["script"]))  # noqa: E731
    approver = (lambda payload: False) if args.deny else (lambda payload: True)

    results = [run_case(inc, approver, model_factory) for inc in INCIDENTS]
    completed = [r for r in results if r["status"] == r["expected"]]
    latencies = sorted(r["latency_ms"] for r in results)

    def pct(values, p):
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, round(p / 100 * len(values)) - 1))
        return values[index]

    summary = {
        "model": args.model,
        "tasks": len(results),
        "completion_rate": round(len(completed) / len(results) * 100, 1),
        "avg_supersteps": round(statistics.mean(r["supersteps"] for r in results), 1),
        "avg_model_calls": round(statistics.mean(r["cost"]["model_calls"] for r in results), 1),
        "avg_tool_calls": round(statistics.mean(r["cost"]["tool_calls"] for r in results), 1),
        "avg_tokens": round(statistics.mean(
            r["cost"]["prompt_tokens"] + r["cost"]["completion_tokens"]
            for r in results), 1),
        "human_touch_rate": round(
            sum(1 for r in results if r["interrupts"]) / len(results) * 100, 1),
        "avg_interrupts": round(statistics.mean(r["interrupts"] for r in results), 1),
        "latency_p50_ms": pct(latencies, 50),
        "latency_p95_ms": pct(latencies, 95),
    }

    print(f"model={summary['model']}  tasks={summary['tasks']}"
          f"  deny={args.deny}")
    print(f"完成率        {summary['completion_rate']}%")
    print(f"平均超步数    {summary['avg_supersteps']}")
    print(f"模型调用/任务 {summary['avg_model_calls']}"
          f"  tokens/任务 {summary['avg_tokens']}"
          f"  工具调用/任务 {summary['avg_tool_calls']}")
    print(f"人工介入率    {summary['human_touch_rate']}%"
          f"  中断/任务 {summary['avg_interrupts']}")
    print(f"时延          P50 {summary['latency_p50_ms']}ms"
          f"  P95 {summary['latency_p95_ms']}ms")
    for r in results:
        mark = "OK " if r["status"] == r["expected"] else "MISS"
        print(f"  [{mark}] {r['incident']}  status={r['status']:<10}"
              f" steps={r['supersteps']:<3} interrupts={r['interrupts']}"
              f" {r['latency_ms']}ms")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"summary": summary, "results": results},
                      handle, ensure_ascii=False, indent=2)
        print(f"\n结果已写入 {args.json_out}")
    return 0 if len(completed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
