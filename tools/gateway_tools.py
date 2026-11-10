"""Tool layer: project 1's MCP gateway, imported and driven in-process.

The whole point of project 2's architecture is that the orchestrator does
NOT own tools. It calls the project-01 gateway, so every call inherits:

  * namespaced discovery (logs.* / github.* / notice.*)
  * the three-tier risk policy and the write-operation approval gate
  * the audit trail (arguments hashed, never plaintext)
  * the per-task call budget

The approval gate is what drives the graph's interrupt: when the gateway
raises ApprovalRequired, the node surfaces it to a human via
interrupt() and only retries with approved=True after an explicit yes.
Policy lives in exactly one place.

For unit tests, `FakeGateway` reproduces the gateway's contract
(call / ApprovalRequired / PermissionDenied / BudgetExceeded) over an
in-process copy of the logs-server data, so scenario semantics can be
tested without spawning subprocesses.
"""

import importlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_P02 = os.path.dirname(_HERE)
_P01 = os.environ.get("PROJECT01_ROOT") or os.path.normpath(
    os.path.join(_P02, "..", "project-01-mcp-gateway"))


def _load_project01():
    """Import the gateway package from the sibling repo (once)."""
    if _P01 not in sys.path:
        sys.path.insert(0, _P01)
    return importlib.import_module("gateway.gateway")


class ToolApprovalNeeded(Exception):
    """Gateway said 'needs approval' -- the graph must interrupt."""

    def __init__(self, inner):
        self.tool = inner.tool
        self.risk = inner.risk
        self.preview = inner.preview
        self.inner = inner
        super().__init__(str(inner))


class ToolFailure(Exception):
    pass


class GatewayTools:
    """Thin façade the scenario nodes talk to."""

    PRINCIPAL = "agent"

    def __init__(self, gateway):
        self.gateway = gateway

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls, audit_path=None, servers=None, policies=None,
              risk_registry=None, max_calls_per_task=20):
        # The gateway spawns its servers as `python -m <pkg>.<mod>` with a
        # single cwd; P02's logs server and P01's github/notice servers live
        # in different roots, so both roots must be importable.
        existing = os.environ.get("PYTHONPATH", "")
        parts = [p for p in (_P02, _P01) if not existing or p not in existing.split(os.pathsep)]
        os.environ["PYTHONPATH"] = os.pathsep.join(parts + ([existing] if existing else []))
        os.environ.setdefault("PROJECT01_ROOT", _P01)
        mod = _load_project01()
        servers = servers or {
            "logs": ["-m", "mcpservers.logs_server"],
            "github": ["-m", "servers.github_server"],
            "notice": ["-m", "servers.notice_server"],
        }
        policies = policies or {
            # the agent is read-mostly: writes are possible but gated
            "agent": {"max_risk": "admin",
                      "allow": ["logs.query_logs", "logs.service_status",
                                "logs.recent_incidents", "logs.restart_service",
                                "github.search_issues", "github.get_issue",
                                "notice.search_notices", "notice.publish_notice"]},
            "observer": {"max_risk": "read", "allow": ["*"]},
        }
        risk_registry = risk_registry or {
            "logs.restart_service": "admin",
            "notice.publish_notice": "admin",
        }
        audit = audit_path or os.path.join(_P02, "gateway_audit.jsonl")
        gateway = mod.Gateway(
            servers, policies, risk_registry, cwd=_P02, audit_path=audit,
            max_calls_per_task=max_calls_per_task).start()
        return cls(gateway)

    # -- calling -----------------------------------------------------------
    def call(self, name, arguments=None, approved=False):
        mod = self.gateway_module()
        try:
            result = self.gateway.call(self.PRINCIPAL, name, arguments or {},
                                       approved=approved)
        except mod.ApprovalRequired as exc:
            raise ToolApprovalNeeded(exc) from exc
        except (mod.PermissionDenied, mod.BudgetExceeded,
                mod.RpcFailure, mod.TransportError) as exc:
            raise ToolFailure(f"{name}: {exc}") from exc
        return self._payload(result)

    def list_tools(self):
        return self.gateway.list_tools(self.PRINCIPAL)

    def reset_task(self):
        self.gateway.reset_task()

    def stop(self):
        self.gateway.stop()

    _MODULE = None

    @classmethod
    def gateway_module(cls):
        if cls._MODULE is None:
            cls._MODULE = _load_project01()
        return cls._MODULE

    @staticmethod
    def _payload(result):
        """Machine-readable first: structuredContent; fall back to the text
        block only when it is intact (project 1's truncation lesson)."""
        if "structuredContent" in result:
            return result["structuredContent"]
        blocks = result.get("content", [])
        if blocks and blocks[0].get("type") == "text":
            text = blocks[0]["text"]
            if result.get("_truncated"):
                raise ToolFailure("text result was truncated; refuse to parse")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
        return {}

    # -- fake for tests ----------------------------------------------------
class FakeGateway:
    """In-process stand-in: the logs server's own handlers, gateway-shaped
    exceptions, same call() contract. Only the tools the scenario uses."""

    class ApprovalRequired(Exception):
        def __init__(self, tool, risk, preview):
            self.tool, self.risk, self.preview = tool, risk, preview
            super().__init__(f"{tool} requires approval (risk={risk})")

    class PermissionDenied(Exception):
        pass

    class BudgetExceeded(Exception):
        pass

    def __init__(self, principal="agent", max_calls_per_task=20):
        sys.path.insert(0, _P02)
        module = importlib.import_module("mcpservers.logs_server")
        self._mod = module
        self._mod._reset_runtime()
        self.max_calls_per_task = max_calls_per_task
        self._calls = 0
        self.audit = []
        self.PRINCIPAL = principal

    def call(self, principal, name, arguments=None, approved=False):
        handlers = {
            "logs.query_logs": (self._mod.query_logs, "read", False),
            "logs.service_status": (self._mod.service_status, "read", False),
            "logs.recent_incidents": (self._mod.recent_incidents, "read", False),
            "logs.restart_service": (self._mod.restart_service, "admin", True),
            "github.search_issues": (self._search_issues, "read", False),
            "notice.search_notices": (self._search_notices, "read", False),
            "notice.publish_notice": (self._publish, "admin", True),
        }
        if name not in handlers:
            raise ValueError(f"unknown tool: {name}")
        handler, risk, needs_approval = handlers[name]
        if needs_approval and not approved:
            preview = json.dumps(arguments or {}, ensure_ascii=False)[:200]
            self.audit.append((name, "needs_approval"))
            raise self.ApprovalRequired(name, risk, preview)
        if self._calls >= self.max_calls_per_task:
            self.audit.append((name, "budget_exceeded"))
            raise self.BudgetExceeded(f"task call budget {self.max_calls_per_task} exhausted")
        self._calls += 1
        self.audit.append((name, "allowed"))
        payload = handler(**(arguments or {}))
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                "structuredContent": payload}

    def reset_task(self):
        self._calls = 0

    def stop(self):
        pass

    # deterministic stand-ins for the two external-ish servers
    KNOWN_ISSUES = [
        {"number": 4821, "title": "auth: memory leak in token cache (fixed in 1.9.0)",
         "state": "open"},
        {"number": 3902, "title": "course-api: pool sizing regression", "state": "open"},
    ]

    def _search_issues(self, keyword="", limit=5):
        hits = [i for i in self.KNOWN_ISSUES if keyword.lower() in i["title"].lower()]
        return {"total": len(hits), "items": hits[:limit], "_meta": {"cache": "fake"}}

    def _search_notices(self, keyword="", tag=None):
        return {"count": 0, "notices": []}

    def _publish(self, title="", body="", tag="综合", confirm=False):
        return {"ok": bool(confirm), "notice": {"title": title, "body": body, "tag": tag}}


class FakeGatewayTools(GatewayTools):
    """GatewayTools over the FakeGateway (same façade, no subprocesses)."""

    def __init__(self, fake=None):
        super().__init__(fake or FakeGateway())

    @classmethod
    def build(cls, **_kwargs):                       # pragma: no cover
        return cls()

    def call(self, name, arguments=None, approved=False):
        try:
            result = self.gateway.call(self.PRINCIPAL, name, arguments or {},
                                       approved=approved)
        except FakeGateway.ApprovalRequired as exc:
            raise ToolApprovalNeeded(exc) from exc
        except (FakeGateway.PermissionDenied, FakeGateway.BudgetExceeded) as exc:
            raise ToolFailure(str(exc)) from exc
        return self._payload(result)

    def stop(self):
        pass
