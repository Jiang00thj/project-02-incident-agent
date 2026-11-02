"""Scenario nodes: supervisor -> sub-agents -> reflection -> approval.

Every node is a closure over two injected dependencies:

    model   -- anything with .decide(node, state) -> dict (see model/)
    tools   -- GatewayTools / FakeGatewayTools (see tools/)

The interrupt flow is the part to study: `await_approval` calls the
gateway WITHOUT approved=True on purpose. The gateway's own approval gate
raises, the node converts that into an interrupt() for a human, and only
the human's yes retries the call with approved=True. Replaying the node
re-executes the first (denied) call too -- which the gateway logs as
needs_approval both times, so the audit trail stays truthful.

Idempotency (the interview question): if the process crashes *between*
the human's yes and the tool call, the replay re-runs the whole node from
the pre-write state. Any write tool must therefore be idempotent or
compensable -- logs.restart_service reports already_restarted instead of
restarting twice, and that is not an accident.
"""

import time

from minigraph import Command, interrupt
from tools.gateway_tools import ToolApprovalNeeded, ToolFailure

MAX_ITERATIONS = 2
COST_KEYS = ("model_calls", "prompt_tokens", "completion_tokens")


def make_nodes(model, tools):
    """Return {node_name: callable} wired to the given model and tools."""

    def _decide(node, state):
        """Call the model and return (answer, usage_delta_for_this_call).

        The delta matters: model.usage is cumulative for the whole process,
        but the state's cost channel *sums* node updates -- feeding it
        cumulative numbers would double-count every earlier node.
        """
        before = dict(getattr(model, "usage", {}) or {})
        answer = model.decide(node, state)
        after = getattr(model, "usage", {}) or {}
        delta = {key: after.get(key, 0) - before.get(key, 0)
                 for key in COST_KEYS if isinstance(after.get(key), (int, float))}
        return answer, delta

    # ---------------------------------------------------------------- intake
    def intake(state):
        decision, cost = _decide("intake", state)
        route = decision.get("route", "scan_logs")
        if isinstance(route, str):
            route = [route]
        route = [r for r in route if r in ("scan_logs", "check_known")] or ["scan_logs"]
        return Command(
            update={
                "messages": [f"[intake] 事件 {state['incident'].get('id')} "
                             f"路由到 {route}（并行子代理）"],
                "cost": cost,
            },
            goto=route,
        )

    # ------------------------------------------------------- parallel probes
    def scan_logs(state):
        decision, cost = _decide("scan_logs", state)
        service = state["incident"].get("service", "auth")
        args = {"service": service, "limit": 20}
        if decision.get("level"):
            args["level"] = decision["level"]
        if decision.get("keyword"):
            args["keyword"] = decision["keyword"]
        payload = tools.call("logs.query_logs", args)
        lines = payload.get("logs", [])
        cost["tool_calls"] = 1
        return {
            "messages": [f"[scan_logs] {service} 取回 {len(lines)} 行 "
                         f"(level={args.get('level')})"],
            "findings": [{"source": "logs", "service": service, "lines": lines}],
            "cost": cost,
        }

    def check_known(state):
        decision, cost = _decide("check_known", state)
        service = state["incident"].get("service", "auth")
        keyword = decision.get("keyword") or f"{service} regression"
        payload = tools.call("github.search_issues", {"keyword": keyword, "limit": 5})
        items = payload.get("items", [])
        cost["tool_calls"] = 1
        return {
            "messages": [f"[check_known] GitHub 检索 '{keyword}' 命中 {len(items)} 条"],
            "findings": [{"source": "github", "items": items}],
            "cost": cost,
        }

    # ---------------------------------------------------------------- think
    def diagnose(state):
        decision, cost = _decide("diagnose", state)
        hypothesis = {
            "root_cause": decision.get("root_cause", "unknown"),
            "fix_action": decision.get("fix_action", "escalate"),
            "confidence": float(decision.get("confidence", 0.0)),
            "service": decision.get("service") or state["incident"].get("service", "auth"),
        }
        goto = "await_approval" if hypothesis["fix_action"] == "restart" else "escalate"
        return Command(
            update={
                "messages": [f"[diagnose] 根因={hypothesis['root_cause']} "
                             f"置信={hypothesis['confidence']} 处置={hypothesis['fix_action']}"],
                "hypothesis": hypothesis,
                "cost": cost,
            },
            goto=goto,
        )

    # ---------------------------------------------------------------- HITL
    def await_approval(state):
        hypothesis = state["hypothesis"]
        service = hypothesis.get("service", "auth")
        reason = hypothesis.get("root_cause", "")
        try:
            # Deliberately NOT approved=True: let the gateway's gate speak.
            tools.call("logs.restart_service", {"service": service, "reason": reason})
        except ToolApprovalNeeded as exc:
            approved = interrupt({
                "question": f"是否重启 {service} 以处置事件？",
                "tool": exc.tool, "risk": exc.risk, "args_preview": exc.preview,
                "root_cause": reason,
            })
            if not approved:
                return Command(update={
                    "messages": [f"[approval] 人工拒绝了 {exc.tool}，转入升级"],
                    "status": "denied",
                    "cost": {"denials": 1},
                }, goto="escalate")
            result = tools.call("logs.restart_service",
                                {"service": service, "reason": reason}, approved=True)
            return Command(update={
                "messages": [f"[approval] 人工批准并执行 {exc.tool}："
                             f"already_restarted={result.get('already_restarted')}"],
                "cost": {"approvals": 1, "tool_calls": 1},
            }, goto="apply_fix")
        except ToolFailure as exc:                       # e.g. budget exhausted
            return Command(update={"messages": [f"[approval] 工具失败：{exc}"]},
                           goto="escalate")
        # The gateway did not gate this write (misconfigured risk table):
        # refuse to silently execute an ungated admin action.
        return Command(update={"messages": ["[approval] 写操作未被网关拦截，拒绝执行"]},
                       goto="escalate")

    def apply_fix(state):
        service = state["hypothesis"].get("service", "auth")
        status = tools.call("logs.service_status", {"service": service})
        return {
            "messages": [f"[apply_fix] {service} 状态={status.get('status')} "
                         f"重启次数={status.get('restarts')}"],
            "cost": {"tool_calls": 1},
        }

    # ----------------------------------------------------------- reflection
    def verify(state):
        service = state["hypothesis"].get("service", "auth")
        status = tools.call("logs.service_status", {"service": service})
        healthy = status.get("status") == "running" and not status.get("open_incident")
        iteration = state.get("iteration", 0) + 1
        if healthy:
            return Command(update={
                "messages": [f"[verify] 第 {iteration} 轮验证通过：{service} 恢复"],
                "iteration": iteration, "status": "fixed",
                "cost": {"tool_calls": 1},
            }, goto="report")
        if iteration < MAX_ITERATIONS:
            return Command(update={
                "messages": [f"[verify] 第 {iteration} 轮未恢复，回到日志复查（反思循环）"],
                "iteration": iteration, "cost": {"tool_calls": 1},
            }, goto="scan_logs")
        return Command(update={
            "messages": [f"[verify] {MAX_ITERATIONS} 轮未恢复，停止重试并升级"],
            "iteration": iteration, "status": "escalated", "cost": {"tool_calls": 1},
        }, goto="escalate")

    # ---------------------------------------------------------------- exits
    def escalate(state):
        service = state["incident"].get("service", "?")
        status = "denied" if state.get("status") == "denied" else "escalated"
        return Command(update={
            "status": status,
            "messages": [f"[escalate] {service} 移交人工值班：根因="
                         f"{state.get('hypothesis', {}).get('root_cause', 'unknown')}"],
        }, goto="report")

    def report(state):
        service = state["incident"].get("service", "?")
        decision, cost = _decide("report", state)
        title = decision.get("postmortem_title", f"事件复盘 {state['incident'].get('id')}")
        body = decision.get("postmortem_body", "")
        published = None
        if state.get("status") != "running":
            try:
                tools.call("notice.publish_notice",
                           {"title": title, "body": body, "confirm": False})
            except ToolApprovalNeeded as exc:
                approved = interrupt({
                    "question": f"是否发布复盘通知《{title}》？",
                    "tool": exc.tool, "risk": exc.risk, "args_preview": exc.preview,
                })
                if approved:
                    result = tools.call("notice.publish_notice",
                                        {"title": title, "body": body, "confirm": True},
                                        approved=True)
                    published = bool(result.get("ok"))
        cost["tool_calls"] = 1 if published is not None else 0
        if published:
            cost["approvals"] = 1
        return {
            "status": state.get("status", "running"),
            "messages": [f"[report] 完成，复盘{'已发布' if published else '未发布'}"],
            "outcome": {
                "incident": state["incident"].get("id"),
                "service": service,
                "status": state.get("status"),
                "root_cause": state.get("hypothesis", {}).get("root_cause"),
                "postmortem_published": published,
                "cost": dict(state.get("cost", {})),
                "finished_at": time.strftime("%H:%M:%S"),
            },
            "cost": cost,
        }

    return {
        "intake": intake, "scan_logs": scan_logs, "check_known": check_known,
        "diagnose": diagnose, "await_approval": await_approval,
        "apply_fix": apply_fix, "verify": verify, "escalate": escalate,
        "report": report,
    }
