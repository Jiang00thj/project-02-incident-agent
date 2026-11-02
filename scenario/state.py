"""Scenario state schema.

Two things worth noticing:

  * messages / findings are `Annotated[list, add]` channels because the
    supervisor fans out two sub-agents in one superstep; both append.
  * cost uses a *custom* reducer that sums dicts key-wise -- parallel
    sub-agents each burn tokens, and a last-value channel would silently
    drop one of the two bills.
"""

from typing import Annotated, TypedDict

from minigraph import add


def merge_cost(left, right):
    left = dict(left or {})
    for key, value in (right or {}).items():
        if isinstance(value, (int, float)) and isinstance(left.get(key), (int, float)):
            left[key] = left[key] + value
        else:
            left[key] = value
    return left


class TriageState(TypedDict):
    incident: dict                     # the report that came in
    messages: Annotated[list, add]     # human-readable run trace
    findings: Annotated[list, add]     # structured evidence (logs, known issues)
    hypothesis: dict                   # root cause + proposed fix
    iteration: int                     # reflect-loop counter
    status: str                        # running | fixed | escalated | denied
    cost: Annotated[dict, merge_cost]  # model + tool + approval accounting
    outcome: dict                      # final report for the caller


INITIAL = {
    "incident": {},
    "messages": [],
    "findings": [],
    "hypothesis": {},
    "iteration": 0,
    "status": "running",
    "cost": {"model_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "tool_calls": 0, "approvals": 0, "denials": 0},
    "outcome": {},
}
