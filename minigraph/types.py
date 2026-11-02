"""Core types for minigraph: reducers, commands, errors, node policies.

Nothing here imports anything outside the standard library. The naming
follows LangGraph on purpose so that the semantics we implement can be
compared 1:1 against the real thing (see adapters/langgraph_adapter.py).
"""

from dataclasses import dataclass, field
from typing import Any, Callable


# ------------------------------------------------------------------ reducers

def add(left, right):
    """List-concatenating reducer, the `Annotated[list, add]` of LangGraph."""
    if left is None and right is None:
        return []
    if left is None:
        return list(right)
    if right is None:
        return list(left)
    return list(left) + list(right)


REPLACE = None  # sentinel: last write wins (LangGraph's LastValue channel)


# ------------------------------------------------------------------ commands

@dataclass
class Command:
    """A node's combined "update state + choose next node" answer.

    Mirrors langgraph.types.Command. `resume` is only used by the *caller*
    of invoke() to answer a pending interrupt.
    """

    update: dict = field(default_factory=dict)
    goto: Any = None            # node name, list of names, or END
    resume: Any = None          # value fed back into interrupt() on replay
    dag: Any = None             # unsupported, kept for API familiarity


# ------------------------------------------------------------------ errors

class GraphBubbleUp(Exception):
    """Base for control-flow signals that must escape node code."""


class GraphInterrupt(GraphBubbleUp):
    """Raised by interrupt() when no resume value is pending.

    The graph stops at the superstep boundary *before* merging the node's
    partial writes, so a replay re-runs the whole node deterministically.
    """

    def __init__(self, payload):
        self.payload = payload
        super().__init__(f"interrupt: {payload!r}")


class GraphRecursionError(RuntimeError):
    """Superstep budget exhausted (LangGraph's recursion_limit)."""


class CycleDetected(RuntimeError):
    """The same (state, active nodes) signature repeated too many times.

    LangGraph only has a step budget; detecting *state* cycles is the
    cheap differentiator this project adds -- an agent stuck calling the
    same tool with the same arguments burns money forever without ever
    tripping a step limit that counts *distinct* progress.
    """

    def __init__(self, signature, repeats):
        self.signature = signature
        self.repeats = repeats
        nodes = ", ".join(sorted(signature[1])) or "<none>"
        super().__init__(
            f"state cycle: nodes [{nodes}] produced identical state {repeats} times; "
            f"digest={signature[0][:12]}"
        )


class InvalidUpdateError(RuntimeError):
    """Two parallel nodes wrote the same last-value field in one superstep."""


class NodeTimeoutError(RuntimeError):
    """A node exceeded its wall-clock budget; its writes were discarded."""

    def __init__(self, node, elapsed, kind="run"):
        self.node = node
        self.elapsed = elapsed
        self.kind = kind
        super().__init__(f"node {node!r} {kind} timeout after {elapsed:.2f}s")


# ------------------------------------------------------------------ policies
# LangGraph 1.2 (2026-05) added per-node retry/timeout/error_handler plus
# set_node_defaults. We mirror those knobs so lessons transfer directly.

@dataclass
class RetryPolicy:
    max_attempts: int = 2
    backoff_s: float = 0.0     # linear backoff; keep 0 in tests


@dataclass
class TimeoutPolicy:
    run_timeout: float = None  # hard wall clock for one node execution


# ------------------------------------------------------------------ interrupt()

_PENDING_RESUME = []  # stack: values to feed back into interrupt() on replay


def interrupt(payload):
    """Pause the graph and hand `payload` to a human.

    First run: raises GraphInterrupt (graph checkpoints and stops).
    Replay: returns the value the human supplied via Command(resume=...).
    """
    if _PENDING_RESUME:
        return _PENDING_RESUME.pop(0)
    raise GraphInterrupt(payload)


def _feed_resume(value):
    _PENDING_RESUME.append(value)


def _clear_resume():
    _PENDING_RESUME.clear()
