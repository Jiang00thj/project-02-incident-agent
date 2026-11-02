"""Incident suite: reproducible inputs + scripted model behavior.

Each incident bundles:
  * the incident report (what the on-call saw),
  * the scripted model answers (what a competent SRE agent would decide),
  * the expected terminal status (what "correct" means for the bench).

INC-2403 is the interesting one: the agent wrongly believes a restart
will fix a connection-pool regression, hits the reflect loop twice, then
escalates. It exercises retry, iteration budget and the escalation exit
in one deterministic run.
"""

INCIDENTS = [
    {
        "id": "INC-2401",
        "incident": {"id": "INC-2401", "service": "auth", "severity": "high",
                     "symptom": "登录接口 5 分钟内 5xx 占比 34%",
                     "reported_at": "2026-11-02T10:15:00Z"},
        "script": {
            "intake": [{"route": ["scan_logs", "check_known"], "severity": "high"}],
            "scan_logs": [{"level": "ERROR"}],
            "check_known": [{"keyword": "memory leak"}],
            "diagnose": [{"root_cause": "auth 1.8.2 token 缓存内存泄漏，OOM 循环重启",
                          "fix_action": "restart", "confidence": 0.92, "service": "auth"}],
            "report": [{"postmortem_title": "INC-2401 auth OOM 循环复盘",
                        "postmortem_body": "auth 1.8.2 内存泄漏触发 OOM 循环；重启止血，"
                                           "升级到 1.9.0 才是根治。"}],
        },
        "expected_status": "fixed",
    },
    {
        "id": "INC-2402",
        "incident": {"id": "INC-2402", "service": "course-api", "severity": "medium",
                     "symptom": "选课接口 P95 从 120ms 涨到 3.1s",
                     "reported_at": "2026-11-02T10:16:00Z"},
        "script": {
            "intake": [{"route": ["scan_logs", "check_known"], "severity": "medium"}],
            "scan_logs": [{"level": "WARN"}],
            "check_known": [{"keyword": "pool sizing"}],
            "diagnose": [{"root_cause": "course-api 3.4.0 连接池上限回归（issue #3902），"
                                        "重启无效需发布修复",
                          "fix_action": "escalate", "confidence": 0.88,
                          "service": "course-api"}],
            "report": [{"postmortem_title": "INC-2402 course-api 连接池回归",
                        "postmortem_body": "连接池容量回归是代码缺陷，移交人工发布修复版本。"}],
        },
        "expected_status": "escalated",
    },
    {
        "id": "INC-2403",
        "incident": {"id": "INC-2403", "service": "course-api", "severity": "high",
                     "symptom": "选课大面积超时，疑似资源泄漏",
                     "reported_at": "2026-11-02T10:20:00Z"},
        "script": {
            "intake": [{"route": ["scan_logs", "check_known"], "severity": "high"}],
            "scan_logs": [{"level": "ERROR"}, {"level": "ERROR"}, {"level": "ERROR"}],
            "check_known": [{"keyword": "pool"}],
            # 第一次误判 restart；反思循环第二轮仍 restart；verify 两轮后放弃
            "diagnose": [{"root_cause": "疑似连接池耗尽，先重启观察",
                          "fix_action": "restart", "confidence": 0.55,
                          "service": "course-api"},
                         {"root_cause": "重启后池占用继续攀升，判定代码缺陷",
                          "fix_action": "restart", "confidence": 0.6,
                          "service": "course-api"}],
            "report": [{"postmortem_title": "INC-2403 反思循环两轮后升级",
                        "postmortem_body": "重启两次未能止血，判定为代码缺陷，升级人工处置。"}],
        },
        "expected_status": "escalated",
    },
]


def incident_by_id(inc_id):
    return next(i for i in INCIDENTS if i["id"] == inc_id)
