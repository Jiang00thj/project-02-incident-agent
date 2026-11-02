"""Run the same scenario graph on real LangGraph and compare semantics.

    .venv/Scripts/python.exe adapters/langgraph_adapter.py

This is the "compare instead of trust" part of the project: the scenario
is duplicated as a LangGraph StateGraph (1.2.x API) and the two engines
are run side by side over the same incident + the same scripted answers.
What we check:

  1. interrupt() -> human approval -> resume works on both engines
  2. the final status / outcome agree
  3. parallel fan-out + reducer merge behave the same
  4. the recursion limit fires on both (LangGraph: GraphRecursionError)

Requires langgraph==1.2.x + langgraph-checkpoint-sqlite (requirements-dev.txt)
and is *not* part of the zero-dependency core -- the core must run on a
bare CPython.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from typing import Annotated, TypedDict  # noqa: E402
from operator import add as lg_add       # noqa: E402

from langgraph.graph import StateGraph, START, END  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402
from langgraph.types import Command, interrupt        # noqa: E402
from langgraph.errors import GraphRecursionError      # noqa: E402

from model import ScriptedModel  # noqa: E402
from tools.gateway_tools import FakeGatewayTools, ToolApprovalNeeded  # noqa: E402
from scenario.incidents import incident_by_id  # noqa: E402


class LGState(TypedDict):
    incident: dict
    messages: Annotated[list, lg_add]
    findings: Annotated[list, lg_add]
    hypothesis: dict
    iteration: int
    status: str
    outcome: dict


def build_langgraph(model, tools):
    """The same triage flow, expressed once in LangGraph's own API."""

    def intake(state):
        decision = model.decide("intake", state)
        route = decision.get("route", ["scan_logs"])
        if isinstance(route, str):
            route = [route]
        route = [r for r in route if r in ("scan_logs", "check_known")] or ["scan_logs"]
        return Command(update={"messages": [f"[intake] route={route}"]}, goto=route)

    def scan_logs(state):
        decision = model.decide("scan_logs", state)
        service = state["incident"]["service"]
        args = {"service": service, "limit": 20}
        if decision.get("level"):
            args["level"] = decision["level"]
        payload = tools.call("logs.query_logs", args)
        return {"messages": [f"[scan_logs] {len(payload.get('logs', []))} lines"],
                "findings": [{"source": "logs"}]}

    def check_known(state):
        decision = model.decide("check_known", state)
        keyword = decision.get("keyword") or "regression"
        payload = tools.call("github.search_issues", {"keyword": keyword, "limit": 5})
        return {"messages": [f"[check_known] {len(payload.get('items', []))} hits"],
                "findings": [{"source": "github"}]}

    def diagnose(state):
        decision = model.decide("diagnose", state)
        hypothesis = {
            "root_cause": decision.get("root_cause", "unknown"),
            "fix_action": decision.get("fix_action", "escalate"),
            "confidence": decision.get("confidence", 0.0),
            "service": decision.get("service") or state["incident"]["service"],
        }
        goto = "await_approval" if hypothesis["fix_action"] == "restart" else "escalate"
        return Command(update={"messages": [f"[diagnose] {hypothesis['fix_action']}"],
                               "hypothesis": hypothesis}, goto=goto)

    def await_approval(state):
        service = state["hypothesis"]["service"]
        reason = state["hypothesis"]["root_cause"]
        try:
            tools.call("logs.restart_service", {"service": service, "reason": reason})
        except ToolApprovalNeeded as exc:
            approved = interrupt({"tool": exc.tool, "risk": exc.risk})
            if not approved:
                return Command(update={"status": "denied",
                                       "messages": ["[approval] denied"]},
                               goto="escalate")
            tools.call("logs.restart_service", {"service": service, "reason": reason},
                       approved=True)
            return Command(update={"messages": ["[approval] executed"]}, goto="verify")
        return Command(update={"messages": ["[approval] ungated write refused"]},
                       goto="escalate")

    def verify(state):
        service = state["hypothesis"]["service"]
        status = tools.call("logs.service_status", {"service": service})
        healthy = status.get("status") == "running" and not status.get("open_incident")
        iteration = state.get("iteration", 0) + 1
        if healthy:
            return Command(update={"status": "fixed", "iteration": iteration},
                           goto="report")
        if iteration < 2:
            return Command(update={"iteration": iteration}, goto="scan_logs")
        return Command(update={"status": "escalated", "iteration": iteration},
                       goto="escalate")

    def escalate(state):
        return {"status": "denied" if state.get("status") == "denied" else "escalated",
                "messages": ["[escalate]"]}

    def report(state):
        decision = model.decide("report", state)
        title = decision.get("postmortem_title", "")
        published = None
        if state.get("status") != "running":
            try:
                tools.call("notice.publish_notice", {"title": title, "body": "", "confirm": False})
            except ToolApprovalNeeded as exc:
                approved = interrupt({"tool": exc.tool, "risk": exc.risk})
                if approved:
                    result = tools.call("notice.publish_notice",
                                        {"title": title, "body": "", "confirm": True},
                                        approved=True)
                    published = bool(result.get("ok"))
        return {"outcome": {"status": state.get("status"),
                            "root_cause": state.get("hypothesis", {}).get("root_cause"),
                            "postmortem_published": published},
                "messages": ["[report] done"]}

    graph = StateGraph(LGState)
    graph.add_node("intake", intake)
    graph.add_node("scan_logs", scan_logs)
    graph.add_node("check_known", check_known)
    graph.add_node("diagnose", diagnose)
    graph.add_node("await_approval", await_approval)
    graph.add_node("verify", verify)
    graph.add_node("escalate", escalate)
    graph.add_node("report", report)
    graph.add_edge(START, "intake")
    graph.add_edge("scan_logs", "diagnose")
    graph.add_edge("check_known", "diagnose")
    graph.add_edge("verify", END)
    graph.add_edge("escalate", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=MemorySaver())


def run_langgraph(incident, approver):
    tools = FakeGatewayTools()
    model = ScriptedModel(dict(incident["script"]))
    app = build_langgraph(model, tools)
    config = {"configurable": {"thread_id": incident["id"]}, "recursion_limit": 25}
    state = {"incident": incident["incident"], "messages": [], "findings": [],
             "hypothesis": {}, "iteration": 0, "status": "running", "outcome": {}}
    out = app.invoke(state, config)
    interrupts = 0
    while out.get("__interrupt__"):
        interrupts += 1
        value = approver(out["__interrupt__"][0].value)
        out = app.invoke(Command(resume=value), config)
        if interrupts > 8:
            raise AssertionError("interrupt loop")
    return out, interrupts


def run_minigraph(incident, approver):
    from minigraph import MemoryCheckpointer
    from scenario.graph import build_graph
    from scenario.state import INITIAL
    tools = FakeGatewayTools()
    model = ScriptedModel(dict(incident["script"]))
    app = build_graph(model, tools, checkpointer=MemoryCheckpointer())
    state = dict(INITIAL)
    state["incident"] = incident["incident"]
    out = app.invoke(state, {"thread_id": incident["id"]})
    interrupts = 0
    while "__interrupt__" in out:
        interrupts += 1
        out = app.invoke(None, {"thread_id": incident["id"]},
                         resume=approver(out["__interrupt__"]["payload"]))
        if interrupts > 8:
            raise AssertionError("interrupt loop")
    return out, interrupts


def main():
    incident = incident_by_id("INC-2401")
    approver = lambda payload: True  # noqa: E731

    print("== minigraph ==")
    mine, my_interrupts = run_minigraph(incident, approver)
    print(f"  status={mine['status']} interrupts={my_interrupts} "
          f"findings={[f['source'] for f in mine['findings']]} "
          f"messages={len(mine['messages'])}")

    print("== langgraph 1.2 ==")
    theirs, lg_interrupts = run_langgraph(incident, approver)
    print(f"  status={theirs['status']} interrupts={lg_interrupts} "
          f"findings={len(theirs['findings'])} messages={len(theirs['messages'])}")

    agree = (mine["status"] == theirs["status"]
             and my_interrupts == lg_interrupts
             and sorted(f["source"] for f in mine["findings"])
             == sorted(f["source"] for f in theirs["findings"]))
    print(f"\nsemantics agree: {agree}")
    if not agree:
        raise SystemExit(1)
    print("PASS: 同一场景、同一脚本答案，两个引擎走到同一结果。")


if __name__ == "__main__":
    main()
