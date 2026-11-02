"""Interrupt semantics + checkpoint recovery (the HITL core)."""

import os
import tempfile
import unittest
from typing import Annotated, TypedDict

from minigraph import (StateGraph, START, END, add, interrupt, Command,
                       MemoryCheckpointer, SqliteCheckpointer)


class S(TypedDict):
    msgs: Annotated[list, add]
    approved: bool


def build_risky():
    g = StateGraph(S)

    def risky(state):
        ok = interrupt({"question": "restart?", "risk": "admin"})
        return {"approved": ok, "msgs": [f"approval={ok}"]}

    g.add_node("risky", risky)
    g.add_edge(START, "risky")
    g.add_edge("risky", END)
    return g


class InterruptTest(unittest.TestCase):
    def test_interrupt_surfaces_payload_and_keeps_state(self):
        app = build_risky().compile(checkpointer=MemoryCheckpointer())
        out = app.invoke({"msgs": ["start"], "approved": False}, {"thread_id": "t"})
        self.assertEqual(out["__interrupt__"]["payload"]["risk"], "admin")
        self.assertEqual(out["msgs"], ["start"])

    def test_resume_value_reaches_the_node(self):
        app = build_risky().compile(checkpointer=MemoryCheckpointer())
        app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        out = app.invoke(None, {"thread_id": "t"}, resume=True)
        self.assertTrue(out["approved"])
        self.assertEqual(out["msgs"], ["approval=True"])

    def test_denied_resume_flows_through(self):
        app = build_risky().compile(checkpointer=MemoryCheckpointer())
        app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        out = app.invoke(None, {"thread_id": "t"}, resume=False)
        self.assertFalse(out["approved"])

    def test_command_resume_is_unwrapped(self):
        app = build_risky().compile(checkpointer=MemoryCheckpointer())
        app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        out = app.invoke(None, {"thread_id": "t"}, resume=Command(resume=True))
        self.assertTrue(out["approved"])

    def test_resume_without_value_is_refused(self):
        app = build_risky().compile(checkpointer=MemoryCheckpointer())
        app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        with self.assertRaises(ValueError):
            app.invoke(None, {"thread_id": "t"})

    def test_resume_without_checkpointer_is_refused(self):
        app = build_risky().compile()
        with self.assertRaises(ValueError):
            app.invoke(None, {"thread_id": "t"})

    def test_interrupt_writes_are_not_merged_early(self):
        """A node that appends *before* interrupt() must not leave half of
        its writes in state: the whole node re-runs on resume."""

        g = StateGraph(S)

        def half(state):
            partial = {"msgs": ["half-write"]}
            ok = interrupt({"q": "?"})
            return {**partial, "approved": ok}

        g.add_node("half", half)
        g.add_edge(START, "half")
        g.add_edge("half", END)
        cp = MemoryCheckpointer()
        app = g.compile(checkpointer=cp)
        out = app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        self.assertEqual(out["msgs"], [])          # nothing merged yet
        out = app.invoke(None, {"thread_id": "t"}, resume=True)
        self.assertEqual(out["msgs"], ["half-write"])


class CheckpointRecoveryTest(unittest.TestCase):
    def test_crash_resume_from_sqlite(self):
        workdir = tempfile.mkdtemp()
        path = os.path.join(workdir, "cp.sqlite")
        state_machine = {"b_failed": True}

        g = StateGraph(S)

        def a(state):
            return {"msgs": ["a"]}

        def b(state):
            if state_machine["b_failed"]:
                raise RuntimeError("simulated crash")
            return {"msgs": ["b"]}

        g.add_node("a", a)
        g.add_node("b", b)
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        app = g.compile(checkpointer=SqliteCheckpointer(path))
        with self.assertRaises(RuntimeError):
            app.invoke({"msgs": [], "approved": False}, {"thread_id": "crash"})
        state_machine["b_failed"] = False            # "ops fixed it, restart"
        out = app.invoke(None, {"thread_id": "crash"})
        self.assertEqual(out["msgs"], ["a", "b"])

    def test_sqlite_checkpoints_are_step_addressable(self):
        workdir = tempfile.mkdtemp()
        path = os.path.join(workdir, "cp.sqlite")
        cp = SqliteCheckpointer(path)
        g = StateGraph(S)
        g.add_node("a", lambda s: {"msgs": ["a"]})
        g.add_node("b", lambda s: {"msgs": ["b"]})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        app = g.compile(checkpointer=cp)
        app.invoke({"msgs": [], "approved": False}, {"thread_id": "t"})
        steps = cp.list_steps("t")
        self.assertGreaterEqual(len(steps), 2)       # one row per boundary
        self.assertEqual(steps[-1]["next_nodes"], [])
        cp.close()


if __name__ == "__main__":
    unittest.main()
