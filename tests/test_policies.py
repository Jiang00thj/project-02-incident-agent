"""Node policies: retry, timeout, error_handler, set_node_defaults."""

import time
import unittest
from typing import Annotated, TypedDict

from minigraph import StateGraph, START, END, add, RetryPolicy
from minigraph.types import NodeTimeoutError


class S(TypedDict):
    msgs: Annotated[list, add]
    approved: bool


class PolicyTest(unittest.TestCase):
    def test_retry_recovers_transient_failure(self):
        calls = []

        def flaky(state):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("transient")
            return {"msgs": ["recovered"]}

        g = StateGraph(S)
        g.add_node("f", flaky, retry=RetryPolicy(max_attempts=3))
        g.add_edge(START, "f")
        g.add_edge("f", END)
        out = g.compile().invoke({"msgs": [], "approved": False})
        self.assertEqual(out["msgs"], ["recovered"])
        self.assertEqual(len(calls), 3)

    def test_exhausted_retries_raise(self):
        def dead(state):
            raise ConnectionError("permanent")

        g = StateGraph(S)
        g.add_node("d", dead, retry=RetryPolicy(max_attempts=2))
        g.add_edge(START, "d")
        g.add_edge("d", END)
        with self.assertRaises(ConnectionError):
            g.compile().invoke({"msgs": [], "approved": False})

    def test_error_handler_gets_context_and_routes_on(self):
        def rescue(state):
            assert "__error__" in state, "handler must receive error context"
            return {"msgs": ["rescued:" + state["__error__"]["node"]]}

        g = StateGraph(S)
        g.add_node("dead", lambda s: (_ for _ in ()).throw(ConnectionError("x")),
                   retry=RetryPolicy(max_attempts=2), error_handler="dh")
        g.add_node("dh", rescue)
        g.add_edge(START, "dead")
        g.add_edge("dh", END)
        out = g.compile().invoke({"msgs": [], "approved": False})
        self.assertEqual(out["msgs"], ["rescued:dead"])

    def test_handler_runs_exactly_once(self):
        seen = []

        def rescue(state):
            seen.append(state["__error__"]["node"])
            return {"msgs": ["rescued"]}

        g = StateGraph(S)
        g.add_node("dead", lambda s: (_ for _ in ()).throw(ConnectionError("x")),
                   error_handler="dh")
        g.add_node("dh", rescue)
        g.add_edge(START, "dead")
        g.add_edge("dh", END)
        g.compile().invoke({"msgs": [], "approved": False})
        self.assertEqual(seen, ["dead"])             # not twice: no __error__ KeyError

    def test_timeout_discards_writes(self):
        def slow(state):
            time.sleep(1.5)
            return {"msgs": ["should-not-land"]}

        g = StateGraph(S)
        g.add_node("slow", slow, timeout=0.2)
        g.add_edge(START, "slow")
        g.add_edge("slow", END)
        with self.assertRaises(NodeTimeoutError):
            g.compile().invoke({"msgs": [], "approved": False})

    def test_set_node_defaults_apply_to_later_nodes(self):
        calls = []

        def flaky(state):
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionError("transient")
            return {"msgs": ["ok"]}

        g = StateGraph(S)
        g.set_node_defaults(retry=RetryPolicy(max_attempts=2))
        g.add_node("f", flaky)
        g.add_edge(START, "f")
        g.add_edge("f", END)
        out = g.compile().invoke({"msgs": [], "approved": False})
        self.assertEqual(out["msgs"], ["ok"])
        self.assertEqual(len(calls), 2)

    def test_interrupt_is_not_retried(self):
        from minigraph import interrupt

        attempts = []

        def gating(state):
            attempts.append(1)
            interrupt({"q": "?"})
            return {"msgs": ["after"]}

        g = StateGraph(S)
        g.add_node("g", gating, retry=RetryPolicy(max_attempts=3))
        g.add_edge(START, "g")
        g.add_edge("g", END)
        cp_type = __import__("minigraph", fromlist=["MemoryCheckpointer"]).MemoryCheckpointer
        app = g.compile(checkpointer=cp_type())
        out = app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        self.assertIn("__interrupt__", out)
        self.assertEqual(len(attempts), 1)           # bubble-up, not retry


if __name__ == "__main__":
    unittest.main()
