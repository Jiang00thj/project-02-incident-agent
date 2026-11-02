"""The hand-written orchestration core (~300 lines, zero dependencies).

What this implements, and why each piece exists:

  * StateGraph with reducer-based channels -- `Annotated[list, add]` semantics
    are the difference between "parallel nodes compose" and "parallel nodes
    silently clobber each other".
  * A Pregel/BSP superstep loop: nodes run, writes merge at the boundary,
    then routing decides the next active set. Multiple active nodes run
    concurrently (thread pool), which is where InvalidUpdateError earns
    its keep.
  * Checkpoints at every superstep boundary; interrupt() stops *before*
    the node's writes are merged, so a replay re-executes the whole node
    and stays deterministic.
  * Node policies (retry / timeout / error_handler + set_node_defaults),
    mirroring the knobs LangGraph added in 1.2 (2026-05) so the lessons
    here transfer to the real framework unchanged.
  * Two loop guards: a step budget (recursion_limit, like LangGraph) and
    *state* cycle detection (not in LangGraph) -- the thing that actually
    catches "agent keeps calling the same tool with the same arguments".
"""

import hashlib
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from typing import get_type_hints

from .types import (
    Command, GraphBubbleUp, GraphInterrupt, GraphRecursionError, CycleDetected,
    InvalidUpdateError, NodeTimeoutError, RetryPolicy, TimeoutPolicy,
    _feed_resume, _clear_resume,
)
from .checkpoint import BaseCheckpointer, Checkpoint

START = "__start__"
END = "__end__"
_CYCLE_REPEATS = 3


# ------------------------------------------------------------------ schema

def _resolve_reducers(schema):
    """Accept a TypedDict with `Annotated[T, reducer]` fields, or a plain
    {field: reducer} dict. Fields without a reducer are last-value-wins."""
    reducers = {}
    if isinstance(schema, dict):
        for key, value in schema.items():
            reducers[key] = value if callable(value) else None
        return reducers
    for key, hint in get_type_hints(schema, include_extras=True).items():
        reducer = None
        if hasattr(hint, "__metadata__"):
            for meta in hint.__metadata__:
                if callable(meta):
                    reducer = meta
                    break
        reducers[key] = reducer
    return reducers


class _NodeSpec:
    __slots__ = ("name", "fn", "retry", "timeout", "error_handler")

    def __init__(self, name, fn, retry, timeout, error_handler):
        self.name, self.fn = name, fn
        self.retry, self.timeout = retry, timeout
        self.error_handler = error_handler


# ------------------------------------------------------------------ builder

class StateGraph:
    def __init__(self, schema):
        self._reducers = _resolve_reducers(schema)
        self._nodes = {}
        self._edges = {}        # node -> [next, ...]
        self._cond = {}         # node -> (router, path_map)
        self._entry = None
        self._defaults = {}

    # -- nodes -------------------------------------------------------------
    def add_node(self, name, fn=None, *, retry=None, timeout=None,
                 error_handler=None):
        if fn is None:
            return lambda f: self._register(name, f, retry, timeout, error_handler)
        return self._register(name, fn, retry, timeout, error_handler)

    def _register(self, name, fn, retry, timeout, error_handler):
        self._nodes[name] = _NodeSpec(
            name, fn, retry or self._defaults.get("retry"),
            timeout or self._defaults.get("timeout"),
            error_handler or self._defaults.get("error_handler"))
        return fn

    def set_node_defaults(self, *, retry=None, timeout=None, error_handler=None):
        """Baseline policy for every node; per-node values override."""
        self._defaults = {"retry": retry, "timeout": timeout,
                          "error_handler": error_handler}
        return self

    # -- edges -------------------------------------------------------------
    def add_edge(self, start, end):
        if start == START:
            self._entry = end
            return self
        self._edges.setdefault(start, []).append(end)
        return self

    def add_conditional_edges(self, source, router, path_map=None):
        self._cond[source] = (router, path_map)
        return self

    def set_entry_point(self, name):
        self._entry = name
        return self

    # -- compile -----------------------------------------------------------
    def compile(self, checkpointer=None, recursion_limit=25):
        if not self._entry:
            raise ValueError("no entry point: use add_edge(START, ...) or set_entry_point()")
        for source, targets in self._edges.items():
            if source not in self._nodes:
                raise ValueError(f"edge from unknown node {source!r}")
            for target in targets:
                if target not in self._nodes and target != END:
                    raise ValueError(f"edge to unknown node {target!r}")
        for source, (_, path_map) in self._cond.items():
            if source not in self._nodes:
                raise ValueError(f"conditional edge from unknown node {source!r}")
            for target in (path_map or {}).values():
                if target not in self._nodes and target != END:
                    raise ValueError(f"path_map target {target!r} is not a node")
        return CompiledGraph(self._reducers, self._nodes, self._edges, self._cond,
                             self._entry, checkpointer, recursion_limit)


# ------------------------------------------------------------------ runtime

class CompiledGraph:
    def __init__(self, reducers, nodes, edges, cond, entry,
                 checkpointer, recursion_limit):
        self._reducers = reducers
        self._nodes = nodes
        self._edges = edges
        self._cond = cond
        self._entry = entry
        self.checkpointer = checkpointer
        self.recursion_limit = recursion_limit

    # -- public ------------------------------------------------------------

    def invoke(self, input=None, config=None, *, resume=None):
        """Run to completion, to an interrupt, or from a checkpoint.

        input=None + thread_id  -> resume the latest checkpoint for that
        thread (crash recovery), or replay its interrupted node if a
        `resume` value is supplied (human approval).
        Returns the state dict; an interrupted run carries a
        "__interrupt__" key instead of pretending it finished.
        """
        _clear_resume()
        thread_id = (config or {}).get("thread_id")
        limit = (config or {}).get("recursion_limit") or self.recursion_limit

        if input is None:
            if not thread_id or not isinstance(self.checkpointer, BaseCheckpointer):
                raise ValueError("resuming requires config={'thread_id':...} and a checkpointer")
            cp = self.checkpointer.get_latest(thread_id)
            if cp is None:
                raise ValueError(f"no checkpoint for thread {thread_id!r}")
            state, active, step = cp.state, list(cp.next_nodes), cp.step
            if cp.interrupt:
                if resume is None:
                    raise ValueError(
                        f"thread {thread_id!r} is waiting on an interrupt from node "
                        f"{cp.interrupt['node']!r}; pass resume=<value>")
                value = resume.resume if isinstance(resume, Command) else resume
                _feed_resume(value)
                active = [cp.interrupt["node"]]
        else:
            state = self._merge({}, [(self._entry, dict(input))])
            active, step = [self._entry], 0

        seen = {}
        while active:
            step += 1
            if step > limit:
                raise GraphRecursionError(
                    f"superstep budget {limit} exhausted; active={active}")
            outcomes = self._run_superstep(active, state)

            pending = [(n, out[1]) for n, out in outcomes.items()
                       if out[0] == "interrupt"]
            if pending:
                node, payload = pending[0]
                cp = Checkpoint(thread_id, step, copy.deepcopy(state), [node],
                                {"node": node, "payload": payload})
                self._put(cp)
                return {**copy.deepcopy(state),
                        "__interrupt__": {"node": node, "payload": payload}}

            updates, gotos = [], {}
            for name, outcome in outcomes.items():
                if outcome[0] != "done":
                    continue
                _, update, goto = outcome
                if update:
                    updates.append((name, update))
                if goto is not None:
                    gotos[name] = goto
            state = self._merge(state, updates)

            next_nodes = self._route(active, gotos, state)
            if not next_nodes or next_nodes == {END}:
                self._put(Checkpoint(thread_id, step, copy.deepcopy(state), [], None))
                return state

            active = sorted(next_nodes - {END})
            signature = (self._digest(state), tuple(active))
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] >= _CYCLE_REPEATS:
                raise CycleDetected(signature, seen[signature])
            self._put(Checkpoint(thread_id, step, copy.deepcopy(state), active, None))

        return state

    def get_state(self, thread_id):
        cp = self.checkpointer.get_latest(thread_id) if self.checkpointer else None
        return cp

    # -- superstep ---------------------------------------------------------

    def _run_superstep(self, active, state):
        snapshot = copy.deepcopy(state)
        if len(active) == 1:
            name = active[0]
            return {name: self._run_node(self._nodes[name], snapshot)}
        with ThreadPoolExecutor(max_workers=len(active)) as pool:
            futures = {name: pool.submit(self._run_node, self._nodes[name],
                                         copy.deepcopy(snapshot))
                       for name in active}
            return {name: future.result() for name, future in futures.items()}

    def _run_node(self, spec, snapshot):
        retry = spec.retry if isinstance(spec.retry, RetryPolicy) else (
            RetryPolicy(max_attempts=1) if spec.retry is None else
            RetryPolicy(max_attempts=int(spec.retry)))
        run_timeout = None
        if isinstance(spec.timeout, TimeoutPolicy):
            run_timeout = spec.timeout.run_timeout
        elif isinstance(spec.timeout, (int, float)):
            run_timeout = float(spec.timeout)

        last_error = None
        for attempt in range(max(1, retry.max_attempts)):
            if retry.backoff_s and attempt:
                time.sleep(retry.backoff_s * attempt)
            try:
                update, goto = self._call_fn(spec.fn, snapshot, run_timeout)
                return ("done", update, goto)
            except GraphBubbleUp as signal:      # interrupts are not retryable
                return ("interrupt", signal.payload)
            except NodeTimeoutError as exc:
                last_error = exc
            except Exception as exc:             # noqa: BLE001 - policy boundary
                last_error = exc

        if spec.error_handler is not None:
            handler = self._nodes[spec.error_handler]
            context = {**copy.deepcopy(snapshot),
                       "__error__": {"node": spec.name,
                                     "error": f"{type(last_error).__name__}: {last_error}"}}
            update, goto = self._call_fn(handler.fn, context, None)
            if goto is None:
                # The handler already ran here; continue along *its* edges
                # instead of routing back into it as a fresh node (that
                # would execute the handler twice, second time without the
                # __error__ context).
                goto = self._edges.get(handler.name, []) or END
            return ("done", update or {}, goto)
        raise last_error

    @staticmethod
    def _call_fn(fn, snapshot, run_timeout):
        if run_timeout is None:
            raw = fn(snapshot)
        else:
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(fn, snapshot)
                try:
                    raw = future.result(timeout=run_timeout)
                except TimeoutError as exc:
                    raise NodeTimeoutError(getattr(fn, "__name__", "node"),
                                           time.perf_counter() - started) from exc
        if isinstance(raw, Command):
            return dict(raw.update or {}), raw.goto
        if raw is None:
            return {}, None
        return dict(raw), None

    # -- state merge -------------------------------------------------------

    def _merge(self, state, updates):
        """Fold node updates into state through per-field reducers.

        A last-value field written by two nodes in the same superstep is a
        programming error we refuse to serialize: LangGraph calls this an
        invalid concurrent update, and so do we.
        """
        merged = dict(state)
        writes = {}
        for _node, update in updates:
            for key, value in update.items():
                if key not in self._reducers:
                    raise InvalidUpdateError(
                        f"update to unknown field {key!r}; declare it on the state schema")
                writes.setdefault(key, []).append(value)
        for key, values in writes.items():
            reducer = self._reducers[key]
            if reducer is None:
                if len(values) > 1:
                    raise InvalidUpdateError(
                        f"field {key!r} written by {len(values)} nodes in one "
                        f"superstep; annotate it with a reducer (e.g. Annotated[list, add]) "
                        f"or route the nodes sequentially")
                merged[key] = values[0]
            else:
                acc = merged.get(key)
                for value in values:
                    acc = reducer(acc, value)
                merged[key] = acc
        return merged

    # -- routing -----------------------------------------------------------

    def _route(self, active, gotos, state):
        """Routing runs on the *merged* state of this superstep."""
        next_nodes = set()
        for name in active:
            if name in gotos:
                goto = gotos[name]
                targets = goto if isinstance(goto, (list, tuple, set)) else [goto]
                next_nodes.update(targets)
                continue
            next_nodes.update(self._edges.get(name, []))
            router, path_map = self._cond.get(name, (None, None))
            if router is not None:
                chosen = router(state)
                targets = chosen if isinstance(chosen, (list, tuple, set)) else [chosen]
                for target in targets:
                    if path_map is not None:
                        target = path_map.get(target, target)
                    next_nodes.add(target)
        return next_nodes or {END}

    @staticmethod
    def _digest(state):
        payload = repr(sorted((k, repr(v)) for k, v in state.items()
                              if k != "__interrupt__")).encode()
        return hashlib.sha256(payload).hexdigest()

    def _put(self, cp):
        if isinstance(self.checkpointer, BaseCheckpointer):
            self.checkpointer.put(cp)
