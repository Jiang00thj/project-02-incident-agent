"""MCP server: a small synthetic microservice fleet with log/incident data.

This is project 2's own data plane. It speaks the same hand-written MCP
protocol layer as project 1 (mcp_min is imported from the sibling repo,
so there is exactly one protocol implementation in the story, not two).

The fleet is deliberately small and deterministic:

  * auth 1.8.2     -- memory leak, OOM-kill loop -> fixed by restart
  * course-api     -- connection pool saturation, needs a deploy (restart
                      only clears it temporarily -> agent should escalate)
  * gateway        -- healthy (a red herring for the intake router)

`restart_service` is the write/admin tool. Its side effect is in-process
state, which is exactly what makes interrupt-replay idempotency an honest
question: a restart that already happened must report as a no-op instead
of restarting twice.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_P02 = os.path.dirname(_HERE)
_P01 = os.environ.get("PROJECT01_ROOT") or os.path.normpath(
    os.path.join(_P02, "..", "project-01-mcp-gateway"))
if _P01 not in sys.path:
    sys.path.insert(0, _P01)

from mcp_min.protocol import ToolError  # noqa: E402
from mcp_min.server import MCPServer  # noqa: E402

# ---------------------------------------------------------------- fleet

FLEET = {
    "auth": {"version": "1.8.2", "owner": "platform", "base_status": "degraded"},
    "course-api": {"version": "3.4.0", "owner": "edu", "base_status": "degraded"},
    "gateway": {"version": "0.9.7", "owner": "platform", "base_status": "running"},
}

INCIDENTS = [
    {"id": "INC-2401", "service": "auth", "severity": "high",
     "symptom": "登录接口 5 分钟内 5xx 占比 34%", "kind": "oom_loop"},
    {"id": "INC-2402", "service": "course-api", "severity": "medium",
     "symptom": "选课接口 P95 从 120ms 涨到 3.1s", "kind": "pool_exhaustion"},
]

# Log lines are deterministic. Lines tagged restart=True only exist after a
# restart; lines tagged incident=True vanish once the incident is cleared.
LOGS = [
    {"ts": "10:12:01", "service": "auth", "level": "WARN",
     "message": "rss=1580MB exceeds soft limit 1536MB"},
    {"ts": "10:12:40", "service": "auth", "level": "ERROR",
     "message": "OOMKilled (exit 137), restarting container #7"},
    {"ts": "10:13:05", "service": "auth", "level": "ERROR",
     "message": "OOMKilled (exit 137), restarting container #8"},
    {"ts": "10:13:31", "service": "auth", "level": "ERROR",
     "message": "OOMKilled (exit 137), restarting container #9"},
    {"ts": "10:13:33", "service": "gateway", "level": "WARN",
     "message": "upstream auth unhealthy, circuit breaker half-open"},
    {"ts": "10:14:00", "service": "course-api", "level": "WARN",
     "message": "db pool busy 96/100, waiters=14"},
    {"ts": "10:14:22", "service": "course-api", "level": "ERROR",
     "message": "pool exhausted: acquire timeout after 5000ms"},
    {"ts": "10:14:23", "service": "course-api", "level": "ERROR",
     "message": "pool exhausted: acquire timeout after 5000ms"},
    {"ts": "10:15:00", "service": "gateway", "level": "INFO",
     "message": "config reload complete, 0 errors"},
]

RECOVERY_LINES = {
    "auth": [
        {"ts": "10:16:00", "service": "auth", "level": "INFO",
         "message": "restart complete, rss=410MB, login success rate 99.8%"},
        {"ts": "10:16:01", "service": "auth", "level": "INFO",
         "message": "health check green, circuit breaker closed"},
    ],
    "course-api": [
        {"ts": "10:16:00", "service": "course-api", "level": "WARN",
         "message": "restart cleared waiters but pool usage climbing again (61/100)"},
    ],
}

_RUNTIME = {"restarts": {}, "cleared": set()}


def _reset_runtime():
    """Restore the pristine incident state (tests call this per-case)."""
    _RUNTIME["restarts"] = {}
    _RUNTIME["cleared"].clear()


def _service_status(service):
    info = FLEET.get(service)
    if info is None:
        raise ToolError(f"unknown service: {service}")
    restarts = _RUNTIME["restarts"].get(service, 0)
    cleared = service in _RUNTIME["cleared"]
    incident = next((i for i in INCIDENTS
                     if i["service"] == service and not cleared), None)
    if service == "course-api":
        # pool exhaustion is a code bug: restart buys minutes, not a fix
        status = "degraded" if not cleared else "degraded"
    else:
        status = "running" if (cleared or restarts) else info["base_status"]
    return {
        "service": service, "version": info["version"], "owner": info["owner"],
        "status": status, "restarts": restarts,
        "open_incident": incident["id"] if incident else None,
    }


server = MCPServer(
    name="logs-server",
    version="0.1.0",
    instructions="微服务观测面：日志检索、服务状态、事件列表、重启操作。",
)


@server.tool(
    name="query_logs",
    title="检索服务日志",
    description="【只读】按服务检索日志，可按级别与关键词过滤。重启后的恢复日志也会返回。",
    risk="read",
    annotations={"readOnlyHint": True},
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": ["auth", "course-api", "gateway"]},
            "level": {"type": "string", "enum": ["INFO", "WARN", "ERROR"]},
            "keyword": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["service"],
        "additionalProperties": False,
    },
)
def query_logs(service, level=None, keyword=None, limit=20):
    lines = []
    for entry in LOGS:
        if entry["service"] != service:
            continue
        incident_line = "OOM" in entry["message"] or "pool exhausted" in entry["message"] \
            or "busy" in entry["message"]
        if incident_line and service in _RUNTIME["cleared"]:
            continue
        lines.append(entry)
    if service in _RUNTIME["restarts"]:
        lines.extend(RECOVERY_LINES.get(service, []))
    if level:
        lines = [ln for ln in lines if ln["level"] == level]
    if keyword:
        lines = [ln for ln in lines if keyword in ln["message"]]
    return {"service": service, "count": len(lines), "logs": lines[:limit]}


@server.tool(
    name="service_status",
    title="查询服务状态",
    description="【只读】返回服务版本、健康状态、重启次数与未关闭事件。",
    risk="read",
    annotations={"readOnlyHint": True},
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": ["auth", "course-api", "gateway"]},
        },
        "required": ["service"],
        "additionalProperties": False,
    },
)
def service_status(service):
    return _service_status(service)


@server.tool(
    name="recent_incidents",
    title="列出未关闭事件",
    description="【只读】列出当前观测窗口内未关闭的事件（服务、症状、严重度）。",
    risk="read",
    annotations={"readOnlyHint": True},
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)
def recent_incidents():
    open_now = [i for i in INCIDENTS if i["service"] not in _RUNTIME["cleared"]]
    return {"count": len(open_now), "incidents": open_now}


@server.tool(
    name="restart_service",
    title="重启服务",
    description="【写入】重启指定服务。属于影响面大的操作，必须经过审批。幂等：已重启过的服务"
                "不会再次重启，直接返回当前状态。",
    risk="admin",
    annotations={"readOnlyHint": False, "destructiveHint": True},
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "enum": ["auth", "course-api", "gateway"]},
            "reason": {"type": "string"},
        },
        "required": ["service", "reason"],
        "additionalProperties": False,
    },
)
def restart_service(service, reason):
    if service not in FLEET:
        raise ToolError(f"unknown service: {service}")
    if service in _RUNTIME["restarts"]:
        prior = _service_status(service)
        return {"ok": True, "already_restarted": True, "status": prior}
    _RUNTIME["restarts"][service] = 1
    _RUNTIME["cleared"].add(service)
    return {"ok": True, "already_restarted": False, "status": _service_status(service),
            "reason": reason}


if __name__ == "__main__":
    _reset_runtime()
    server.serve_stdio()
