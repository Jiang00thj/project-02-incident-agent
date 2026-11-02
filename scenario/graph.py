"""Graph assembly: the incident-triage state machine.

    START -> intake --(route, list)--> [scan_logs, check_known]   (fan-out)
    scan_logs  -> diagnose
    check_known -> diagnose                                       (fan-in)
    diagnose --(fix_action)--> await_approval | escalate
    await_approval --(interrupt/resume)--> apply_fix | escalate
    apply_fix -> verify
    verify --(healthy? iteration?)--> report | scan_logs | escalate
    escalate -> report
    report --(interrupt/resume: postmortem)--> END

The fan-out runs the two probes in one superstep; their findings/messages
merge through the `add` reducer and their token bills merge through the
custom `merge_cost` reducer.
"""

from minigraph import StateGraph, START, END
from scenario.state import TriageState
from scenario.nodes import make_nodes


def build_graph(model, tools, checkpointer=None, recursion_limit=25):
    nodes = make_nodes(model, tools)
    graph = StateGraph(TriageState)
    graph.add_node("intake", nodes["intake"])
    graph.add_node("scan_logs", nodes["scan_logs"])
    graph.add_node("check_known", nodes["check_known"])
    graph.add_node("diagnose", nodes["diagnose"])
    graph.add_node("await_approval", nodes["await_approval"])
    graph.add_node("apply_fix", nodes["apply_fix"])
    graph.add_node("verify", nodes["verify"])
    graph.add_node("escalate", nodes["escalate"])
    graph.add_node("report", nodes["report"])

    graph.add_edge(START, "intake")
    graph.add_edge("scan_logs", "diagnose")
    graph.add_edge("check_known", "diagnose")
    graph.add_edge("await_approval", "apply_fix")
    graph.add_edge("apply_fix", "verify")
    graph.add_edge("escalate", "report")
    graph.add_edge("report", END)
    # intake's Command(goto=...) supplies the dynamic routing, so it needs
    # no static edge; diagnose / await_approval / verify also route via
    # Command -- conditional edges would be redundant here.
    return graph.compile(checkpointer=checkpointer, recursion_limit=recursion_limit)
