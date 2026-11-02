"""Scenario-level tests: the triage graph over the fake gateway,
plus one real-gateway integration test (project 01 subprocesses)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from minigraph import MemoryCheckpointer  # noqa: E402
from model import ScriptedModel  # noqa: E402
from tools.gateway_tools import FakeGatewayTools, ToolApprovalNeeded  # noqa: E402
from scenario.graph import build_graph  # noqa: E402
from scenario.state import INITIAL, merge_cost  # noqa: E402
from scenario.incidents import INCIDENTS  # noqa: E402


def run_incident(incident, approver=lambda p: True, tools_class=FakeGatewayTools):
    tools = tools_class()
    model = ScriptedModel(dict(incident["script"]))
    app = build_graph(model, tools, checkpointer=MemoryCheckpointer())
    state = dict(INITIAL)
    state["incident"] = incident["incident"]
    out = app.invoke(state, {"thread_id": incident["id"]})
    interrupts = []
    while "__interrupt__" in out:
        interrupts.append(out["__interrupt__"]["payload"])
        out = app.invoke(None, {"thread_id": incident["id"]},
                         resume=approver(out["__interrupt__"]["payload"]))
        if len(interrupts) > 8:
            raise AssertionError("interrupt loop")
    return out, interrupts, tools


class ScenarioTest(unittest.TestCase):
    def test_fixed_path_with_approvals(self):
        out, interrupts, _ = run_incident(INCIDENTS[0])
        self.assertEqual(out["status"], "fixed")
        self.assertEqual(len(interrupts), 2)          # restart + postmortem
        self.assertTrue(out["outcome"]["postmortem_published"])
        self.assertEqual(out["cost"]["approvals"], 2)

    def test_parallel_probes_merge_findings(self):
        out, _, _ = run_incident(INCIDENTS[0])
        sources = sorted(f["source"] for f in out["findings"])
        self.assertEqual(sources, ["github", "logs"])

    def test_cost_reducer_sums_parallel_bills(self):
        out, _, _ = run_incident(INCIDENTS[0])
        # intake + scan_logs + check_known + diagnose + report = 5 calls
        self.assertEqual(out["cost"]["model_calls"], 5)
        self.assertGreater(out["cost"]["prompt_tokens"], 0)
        self.assertEqual(out["cost"]["tool_calls"], 6)  # logs+github+restart+status+status+publish

    def test_escalation_without_restart_has_no_restart_interrupt(self):
        out, interrupts, _ = run_incident(INCIDENTS[1])
        self.assertEqual(out["status"], "escalated")
        restarts = [p for p in interrupts if "restart" in p["tool"]]
        self.assertEqual(restarts, [])

    def test_reflect_loop_and_idempotent_restart(self):
        out, interrupts, _ = run_incident(INCIDENTS[2])
        self.assertEqual(out["status"], "escalated")
        self.assertEqual(out["iteration"], 2)
        self.assertEqual(len(interrupts), 3)          # 2 restarts + postmortem
        self.assertTrue(any("already_restarted=True" in m for m in out["messages"]))

    def test_denied_restart_escalates_but_publishes_postmortem(self):
        out, interrupts, _ = run_incident(
            INCIDENTS[0], approver=lambda p: "restart" not in p["tool"])
        self.assertEqual(out["status"], "denied")
        self.assertEqual(out["cost"]["denials"], 1)
        self.assertTrue(out["outcome"]["postmortem_published"])

    def test_fake_gateway_approval_gate_fires_before_execution(self):
        tools = FakeGatewayTools()
        with self.assertRaises(ToolApprovalNeeded) as ctx:
            tools.call("logs.restart_service", {"service": "auth", "reason": "x"})
        self.assertEqual(ctx.exception.tool, "logs.restart_service")
        self.assertEqual(ctx.exception.risk, "admin")
        self.assertEqual(tools.gateway.audit.count(("logs.restart_service", "needs_approval")), 1)

    def test_restart_is_idempotent_at_tool_layer(self):
        tools = FakeGatewayTools()
        first = tools.call("logs.restart_service", {"service": "auth", "reason": "x"},
                           approved=True)
        second = tools.call("logs.restart_service", {"service": "auth", "reason": "x"},
                            approved=True)
        self.assertFalse(first["already_restarted"])
        self.assertTrue(second["already_restarted"])
        self.assertEqual(second["status"]["restarts"], 1)

    def test_merge_cost_sums_and_overwrites(self):
        self.assertEqual(merge_cost({"a": 1}, {"a": 2}), {"a": 3})
        self.assertEqual(merge_cost(None, {"b": 5}), {"b": 5})
        self.assertEqual(merge_cost({"x": "label"}, {"x": "other"}), {"x": "other"})

    def test_scripted_model_repeats_last_answer(self):
        model = ScriptedModel({"diagnose": [{"a": 1}, {"a": 2}]})
        self.assertEqual(model.decide("diagnose", {})["a"], 1)
        self.assertEqual(model.decide("diagnose", {})["a"], 2)
        self.assertEqual(model.decide("diagnose", {})["a"], 2)   # repeats
        self.assertEqual(model.decide("unknown_node", {}), {})


class RealGatewayIntegrationTest(unittest.TestCase):
    """One end-to-end pass through project 1's actual gateway (subprocess
    MCP servers). Skipped when the sibling repo is absent."""

    @classmethod
    def setUpClass(cls):
        p01 = os.environ.get("PROJECT01_ROOT") or os.path.join(
            os.path.dirname(ROOT), "project-01-mcp-gateway")
        if not os.path.isdir(p01):
            raise unittest.SkipTest("project-01 not found")
        from tools.gateway_tools import GatewayTools
        cls.audit_path = os.path.join(ROOT, "gateway_audit.jsonl")
        if os.path.exists(cls.audit_path):
            os.remove(cls.audit_path)
        cls.tools = GatewayTools.build(audit_path=cls.audit_path)

    @classmethod
    def tearDownClass(cls):
        cls.tools.stop()

    def test_full_triage_over_real_gateway(self):
        incident = INCIDENTS[0]
        model = ScriptedModel(dict(incident["script"]))
        app = build_graph(model, self.tools, checkpointer=MemoryCheckpointer())
        state = dict(INITIAL)
        state["incident"] = incident["incident"]
        out = app.invoke(state, {"thread_id": "it-2401"})
        self.assertEqual(out["__interrupt__"]["payload"]["tool"], "logs.restart_service")
        out = app.invoke(None, {"thread_id": "it-2401"}, resume=True)
        out = app.invoke(None, {"thread_id": "it-2401"}, resume=True)
        self.assertEqual(out["status"], "fixed")
        # the audit trail must show the gate doing its job
        import json
        with open(self.audit_path, encoding="utf-8") as handle:
            records = [json.loads(l) for l in handle if l.strip()]
        decisions = {r["decision"] for r in records}
        self.assertIn("needs_approval", decisions)
        self.assertIn("allowed", decisions)
        # and only digests, never plaintext arguments
        for r in records:
            self.assertNotIn("arguments", r)
            self.assertRegex(r["args_digest"], r"^[0-9a-f]{12}$")


if __name__ == "__main__":
    unittest.main()
