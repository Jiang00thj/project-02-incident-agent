"""Model layer: one narrow interface, two implementations.

    model.decide(node, state) -> dict

Every graph node that needs judgment goes through this. ScriptedModel is
the deterministic stand-in that lets tests, demo and bench run offline;
DeepSeekModel is the real thing (OpenAI-compatible chat completions over
stdlib urllib, same shape project 1 used for its benchmarks).

Cost accounting is part of the contract: `decide` records token usage in
`model.usage` so the scenario can report per-task cost without wrapping
anything.
"""

import json
import os
import time
import urllib.request


class ModelError(RuntimeError):
    pass


class ScriptedModel:
    """Queue-per-node deterministic model.

    script = {"intake": [{"route": "logs_first"}, ...], "diagnose": [...]}
    A node's queue is consumed in call order; when it runs dry the last
    response repeats -- a loop that keeps asking the same question gets
    the same answer, which is exactly what should trip the cycle guard.
    """

    COST_PER_CALL = {"prompt_tokens": 650, "completion_tokens": 180}

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.usage = {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.calls = []

    def decide(self, node, state):
        queue = self.script.setdefault(node, [])
        if queue:
            answer = queue.pop(0)
            if not queue:                    # keep the last answer for repeats
                queue.append(answer)
        else:
            answer = {}
        self.usage["model_calls"] += 1
        self.usage["prompt_tokens"] += self.COST_PER_CALL["prompt_tokens"]
        self.usage["completion_tokens"] += self.COST_PER_CALL["completion_tokens"]
        self.calls.append((node, answer))
        return dict(answer)


class DeepSeekModel:
    """OpenAI-compatible DeepSeek adapter (stdlib only, key via env only)."""

    BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    def __init__(self, model=None, api_key=None, timeout=60.0):
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.api_key = api_key or os.environ.get("BENCH_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ModelError("set BENCH_API_KEY (env only, never in files)")
        self.timeout = timeout
        self.usage = {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def decide(self, node, state):
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _system_prompt(node)},
                {"role": "user", "content": json.dumps(
                    {k: v for k, v in state.items() if k in (
                        "incident", "findings", "hypothesis", "iteration", "status")},
                    ensure_ascii=False, default=str)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 512,
            "temperature": 0.0,
        }
        request = urllib.request.Request(
            self.BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode())
        answer = body["choices"][0]["message"].get("content") or "{}"
        usage = body.get("usage", {})
        self.usage["model_calls"] += 1
        self.usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        self.usage["completion_tokens"] += usage.get("completion_tokens", 0)
        self.usage.setdefault("latency_ms", []).append(
            int((time.perf_counter() - started) * 1000))
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as exc:
            raise ModelError(f"model returned invalid JSON for node {node}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ModelError(f"node {node}: expected JSON object, got {type(parsed).__name__}")
        return parsed


def _system_prompt(node):
    prompts = {
        "intake": "你是值班 SRE 主管。根据事件报告决定调查路线，返回 JSON："
                   '{"route": ["scan_logs","check_known"] 或其中之一, "severity": "high|medium|low"}。'
                   "route 用数组表示要并行执行的子任务。",
        "scan_logs": "你是日志分析子代理。根据事件选择日志查询参数，返回 JSON："
                     '{"level": "ERROR|WARN|INFO|null, "keyword": "string 或 null"}。',
        "check_known": "你是已知问题检索子代理。返回 JSON："
                       '{"keyword": "用于 GitHub issue 检索的英文关键词"}。',
        "diagnose": "你是诊断代理。综合日志证据与已知问题，返回 JSON："
                    '{"root_cause": "一句话根因", "fix_action": "restart|escalate", '
                    '"confidence": 0.0-1.0, "service": "服务名"}。'
                    "注意：重启只能解决内存泄漏类问题；连接池耗尽是代码缺陷，重启无效，应 escalate。",
        "report": "你是复盘代理。返回 JSON：{\"postmortem_title\": \"string\", "
                  "\"postmortem_body\": \"3-5 句话复盘\"}。",
    }
    return prompts.get(node, f"你是节点 {node} 的执行代理，返回 JSON 对象。")
