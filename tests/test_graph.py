"""minigraph core semantics: routing, reducers, parallel merge, guards."""

import unittest
from typing import Annotated, TypedDict

from minigraph import StateGraph, START, END, add, Command
from minigraph.types import (InvalidUpdateError, GraphRecursionError,
                             CycleDetected)
from minigraph.checkpoint import MemoryCheckpointer


class S(TypedDict):
    msgs: Annotated[list, add]
    count: int


class GraphSemanticsTest(unittest.TestCase):
    def test_linear_flow_merges_updates(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"msgs": ["a"], "count": 1})
        g.add_node("b", lambda s: {"msgs": ["b"]})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        out = g.compile().invoke({"msgs": [], "count": 0})
        self.assertEqual(out["msgs"], ["a", "b"])
        self.assertEqual(out["count"], 1)

    def test_conditional_routing_on_merged_state(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"count": s["count"] + 1})
        g.add_node("even", lambda s: {"msgs": ["even"]})
        g.add_node("odd", lambda s: {"msgs": ["odd"]})
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "even" if s["count"] % 2 == 0 else "odd")
        g.add_edge("even", END)
        g.add_edge("odd", END)
        out = g.compile().invoke({"msgs": [], "count": 1})
        self.assertEqual(out["msgs"], ["even"])

    def test_path_map_translation(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"count": 1})
        g.add_node("go", lambda s: {"msgs": ["went"]})
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: "yes", {"yes": "go", "no": END})
        g.add_edge("go", END)
        out = g.compile().invoke({"msgs": [], "count": 0})
        self.assertEqual(out["msgs"], ["went"])

    def test_parallel_fanout_appends_in_execution_order(self):
        g = StateGraph(S)
        g.add_node("fan", lambda s: {"msgs": ["fan"]})
        g.add_node("l", lambda s: {"msgs": ["L"]})
        g.add_node("r", lambda s: {"msgs": ["R"]})
        g.add_edge(START, "fan")
        g.add_edge("fan", "l")
        g.add_edge("fan", "r")
        g.add_edge("l", END)
        g.add_edge("r", END)
        out = g.compile().invoke({"msgs": [], "count": 0})
        self.assertEqual(out["msgs"][0], "fan")
        self.assertEqual(set(out["msgs"][1:]), {"L", "R"})

    def test_parallel_write_to_last_value_field_is_rejected(self):
        g = StateGraph(S)
        g.add_node("fan", lambda s: {"msgs": ["fan"]})
        g.add_node("l", lambda s: {"count": 1})
        g.add_node("r", lambda s: {"count": 2})
        g.add_edge(START, "fan")
        g.add_edge("fan", "l")
        g.add_edge("fan", "r")
        g.add_edge("l", END)
        g.add_edge("r", END)
        with self.assertRaises(InvalidUpdateError):
            g.compile().invoke({"msgs": [], "count": 0})

    def test_update_to_undeclared_field_is_rejected(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"nope": 1})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        with self.assertRaises(InvalidUpdateError):
            g.compile().invoke({"msgs": [], "count": 0})

    def test_command_goto_overrides_static_edges(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: Command(update={"count": 1}, goto="c"))
        g.add_node("b", lambda s: {"msgs": ["b"]})
        g.add_node("c", lambda s: {"msgs": ["c"]})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        g.add_edge("c", END)
        out = g.compile().invoke({"msgs": [], "count": 0})
        self.assertEqual(out["msgs"], ["c"])

    def test_recursion_limit_trips(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"count": s["count"] + 1})
        g.add_edge(START, "a")
        g.add_edge("a", "a")
        with self.assertRaises(GraphRecursionError):
            g.compile(recursion_limit=10).invoke({"msgs": [], "count": 0})

    def test_state_cycle_detection_trips_before_step_budget(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {"msgs": []})   # never changes state again
        g.add_edge(START, "a")
        g.add_edge("a", "a")
        with self.assertRaises(CycleDetected):
            g.compile(recursion_limit=50).invoke({"msgs": [], "count": 0})

    def test_compile_requires_entry_point(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {})
        with self.assertRaises(ValueError):
            g.compile()

    def test_compile_rejects_unknown_edge_target(self):
        g = StateGraph(S)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", "ghost")
        with self.assertRaises(ValueError):
            g.compile()

    def test_checkpointer_receives_boundaries(self):
        cp = MemoryCheckpointer()
        g = StateGraph(S)
        g.add_node("a", lambda s: {"msgs": ["a"]})
        g.add_node("b", lambda s: {"msgs": ["b"]})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        g.compile(checkpointer=cp).invoke({"msgs": [], "count": 0}, {"thread_id": "t"})
        latest = cp.get_latest("t")
        self.assertEqual(latest.next_nodes, [])
        self.assertIsNone(latest.interrupt)


if __name__ == "__main__":
    unittest.main()
