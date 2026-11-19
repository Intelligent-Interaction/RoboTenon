"""Resumable next/goto batches and independently scheduled self-loops.

Normal fan-out retains a shared immutable read snapshot and a routing barrier.
Self-loop activations have their own batch, so a slow branch cannot stall them.
A complete set of selected next edges supersedes a target's old self-loop activation.
"""

import asyncio
import inspect
import json
import time

from ..contracts.checkpoint import validate_cursor
from ..contracts.context import GraphContext
from ..contracts.execution import wave_writes
from ..contracts.serialization import jsonable
from ..contracts.workspace import (
    BUSINESS_FIELDS,
    FrozenMap,
    bind_trigger,
    freeze,
    result_view,
)
from .bindings import resolve_bindings
from .conditions import evaluate_condition
from .observability import RoutingObserver
from .result import GraphTransitionRequest


def _predecessors_satisfied(node, incoming, selected=()):
    """Return whether every AND group has an arrived or selected OR member."""

    selected = set(selected)
    return all(
        any(source in selected or incoming.get(source, 0) > 0 for source in group)
        for group in node.predecessor_groups()
    )


class WaveScheduler:
    def __init__(self, compiled, ctx, scope, execute, persist, fail, success, commit, supersede):
        self.graph = compiled.definition
        self.successors = compiled.successors
        self.nodes = self.graph.node_map()
        self.ctx, self.execute, self.persist = ctx, execute, persist
        self.scope = scope
        self.fail, self.success, self.commit = fail, success, commit
        self.supersede = supersede
        self.tasks = {}
        self._frozen_records = {}
        self._frozen_outputs = {}
        # Monotonic deadlines are process-local. Recovery starts a fresh period
        # for pending loops rather than replaying ticks missed during downtime.
        self.loop_anchors = {}
        self.not_before = {}
        self.last_starts = {}
        self.last_ticks = {}
        self.state = ctx.cursors.setdefault(
            scope,
            {
                "pending": self.graph.entry_nodes(),
                "pending_triggers": {},
                "completed": [],
                "activated": [],
                "waiting": [],
                "wave": [],
                "done": [],
                "executions": 0,
                "terminal": False,
                "finished": False,
                "generation": 0,
                "patch_claims": {},
                "arrivals": {},
                "batches": {},
                "epochs": {},
                "loop_sources": {},
                "routed_epochs": {},
                "waiting_diagnostics": [],
            },
        )
        validate_cursor(self.state, self.nodes)
        if "batches" not in self.state and self.state["wave"]:
            raise ValueError(
                "old active routing checkpoint; explicitly start a new run"
            )
        self.state.setdefault("batches", {})
        self.state.setdefault("pending_triggers", {})
        self.state.setdefault("epochs", {})
        self.state.setdefault("loop_sources", {})
        self.state.setdefault("routed_epochs", {})
        self.observer = RoutingObserver(ctx, self.graph, scope, self.state)
        for node_id, trigger in self.state["pending_triggers"].items():
            if trigger == "loop":
                self.not_before[node_id] = (
                    time.monotonic() + self.nodes[node_id].loop.interval_seconds()
                )

    @staticmethod
    def _snapshot_published_values(source, cache):
        """Reuse frozen values while retaining a fresh immutable outer mapping."""
        frozen = {}
        current = {}
        for key, value in source.items():
            cached = cache.get(key)
            if cached is not None and cached[0] is value:
                frozen_value = cached[1]
            else:
                frozen_value = freeze(value)
            frozen[key] = frozen_value
            current[key] = (value, frozen_value)
        return FrozenMap._from_frozen(frozen), current

    async def _persist_checkpoint(self):
        self._refresh_diagnostics()
        result = self.persist()
        if inspect.isawaitable(result):
            await result

    def _refresh_diagnostics(self):
        state = self.state
        diagnostics = []
        for target in state["waiting"]:
            arrived = state["arrivals"].get(target, {})
            missing = {}
            node = self.nodes[target]
            for group in node.predecessor_groups():
                # Once one OR member arrives, the whole group is satisfied;
                # unselected alternatives are not blockers.
                if any(arrived.get(source, 0) for source in group):
                    continue
                for source in group:
                    if source in state["done"]:
                        reason = "awaiting_batch"
                    elif source in state["wave"] or source in state["pending"]:
                        reason = "not_completed"
                    elif state["routed_epochs"].get(source) == state["epochs"].get(
                        source, 0
                    ):
                        reason = "not_selected"
                    else:
                        reason = "not_completed"
                    missing[source] = reason
            diagnostics.append(
                {
                    "node": target,
                    "arrived": [
                        source
                        for source in node.flat_predecessors()
                        if arrived.get(source, 0)
                    ],
                    "missing": missing,
                }
            )
        state["waiting_diagnostics"] = diagnostics

    def _enqueue(self, target, trigger):
        state = self.state
        priorities = {"loop": 0, "goto": 1, "entry": 2, "next": 3}
        previous = state["pending_triggers"].get(target)
        if previous is not None and priorities[previous] >= priorities[trigger]:
            return
        if target not in state["pending"]:
            state["pending"].append(target)
        state["pending_triggers"][target] = trigger
        if trigger == "loop":
            interval = self.nodes[target].loop.interval_seconds()
            now = time.monotonic()
            anchor = self.loop_anchors.get(target, now)
            self.not_before[target] = anchor + (int((now - anchor) / interval) + 1) * interval if interval else now
        else:
            self.not_before.pop(target, None)
            state["loop_sources"].pop(target, None)

    def _batch_for(self, node_id):
        return next(((key, batch) for key, batch in self.state["batches"].items()
                     if node_id in batch["nodes"]), (None, None))

    def _evaluate(self, node, operation, target, expression, phase, trigger, activation, rule_index=0):
        event = dict(node=node, operation=operation, target=target, expression=expression,
                     phase=phase, trigger=trigger, activation=activation, rule_index=rule_index,
                     snapshot_versions=dict(self.ctx.result_versions))
        try:
            matched = evaluate_condition(expression, self.ctx)
        except Exception as exc:
            self.observer.event(**event, matched=None, error=str(exc))
            raise
        self.observer.event(**event, matched=matched)
        return matched

    def _spawn(self, key, batch):
        restored = GraphContext.from_dict(batch["read_fields"])
        fields = {name: object.__getattribute__(restored, name) for name in BUSINESS_FIELDS}
        fields["_outputs"] = freeze(batch["read_outputs"])

        async def execute(node_id):
            trigger = batch["triggers"][node_id]
            started = time.monotonic()
            interval_ms, skipped = None, 0
            if trigger != "loop" or node_id not in self.loop_anchors:
                self.loop_anchors[node_id] = started
                self.last_ticks[node_id] = 0
            else:
                if node_id in self.last_starts:
                    interval_ms = (started - self.last_starts[node_id]) * 1000
                interval = self.nodes[node_id].loop.interval_seconds()
                if interval:
                    tick = int((started - self.loop_anchors[node_id]) / interval)
                    skipped = max(0, tick - self.last_ticks.get(node_id, 0) - 1)
                    self.last_ticks[node_id] = tick
            self.last_starts[node_id] = started
            status = "interrupted"
            try:
                with bind_trigger(self.ctx, trigger):
                    result = await self.execute(self.nodes[node_id], int(key))
                status = "completed" if result is None else result.status
            finally:
                self.observer.timing(
                    self.nodes[node_id],
                    (time.monotonic() - started) * 1000,
                    interval_ms,
                    skipped,
                    trigger,
                    int(key),
                    status,
                )
            # Cancellation is cooperative. Even a plugin swallowing CancelledError
            # cannot publish or route a superseded activation.
            if node_id not in batch["superseded"] and result is None:
                await self.commit(node_id, int(key))
            return result

        with (
            result_view(self.ctx, self.scope, freeze(batch["read_records"]), fields),
            wave_writes(self.state["patch_claims"]),
        ):
            for node_id in batch["nodes"]:
                if (
                    node_id not in self.state["done"]
                    and node_id not in batch["superseded"]
                ):
                    self.tasks[node_id] = asyncio.create_task(execute(node_id))

    async def _launch_pending(self):
        state = self.state
        self._release_waiting()
        now = time.monotonic()
        ready = [
            node
            for node in state["pending"]
            if node not in state["wave"] and self.not_before.get(node, 0) <= now
        ]
        if not ready:
            return
        # Recheck scheduled loop conditions against the exact view used for
        # execution. No await separates this check and snapshot construction.
        records, self._frozen_records = self._snapshot_published_values(
            self.ctx.node_records.get(self.scope, {}), self._frozen_records
        )
        fields = {name: jsonable(object.__getattribute__(self.ctx, name)) for name in BUSINESS_FIELDS}
        outputs, self._frozen_outputs = self._snapshot_published_values(
            object.__getattribute__(self.ctx, "outputs"), self._frozen_outputs
        )
        restored = GraphContext.from_dict(fields)
        condition_fields = {name: object.__getattribute__(restored, name) for name in BUSINESS_FIELDS}
        condition_fields["_outputs"] = outputs
        with result_view(self.ctx, self.scope, freeze(records), condition_fields):
            for node_id in list(ready):
                if state["pending_triggers"].get(node_id) != "loop":
                    continue
                with bind_trigger(self.ctx, state["loop_sources"].get(node_id, "loop")):
                    allowed = self._evaluate(node_id, "loop", node_id, self.nodes[node_id].loop.when,
                                             "before_loop", state["loop_sources"].get(node_id, "loop"),
                                             None)
                if not allowed:
                    ready.remove(node_id)
                    state["pending"].remove(node_id)
                    state["pending_triggers"].pop(node_id)
                    state["loop_sources"].pop(node_id, None)
                    self.not_before.pop(node_id, None)
        if not ready:
            return
        if state["executions"] + len(ready) > self.graph.max_steps:
            raise _StepLimit
        if not state["batches"]:
            state["patch_claims"].clear()
        # Independent self-loops never participate in a slow normal wave barrier.
        normal = [n for n in ready if state["pending_triggers"].get(n, "entry") != "loop"]
        groups = ([normal] if normal else []) + [[n] for n in ready if n not in normal]
        created = []
        for nodes in groups:
            state["generation"] += 1
            key = str(state["generation"])
            batch = {
                "nodes": nodes,
                "triggers": {n: state["pending_triggers"].pop(n, "entry") for n in nodes},
                "superseded": [],
                "epochs": {n: state["epochs"].get(n, 0) for n in nodes},
                "read_records": records,
                "read_fields": fields,
                "read_outputs": outputs,
            }
            state["batches"][key] = batch
            state["wave"].extend(nodes)
            state["executions"] += len(nodes)
            state["activated"] = list(dict.fromkeys(state["activated"] + nodes))
            created.append((key, batch))
        for node_id in ready:
            self.not_before.pop(node_id, None)
            state["loop_sources"].pop(node_id, None)
        state["pending"] = [n for n in state["pending"] if n not in ready]
        await self._persist_checkpoint()
        for key, batch in created:
            self._spawn(key, batch)

    def _retire(self, key):
        batch = self.state["batches"].pop(key)
        for node_id in batch["nodes"]:
            self.tasks.pop(node_id, None)
            self.state["wave"].remove(node_id)
            if node_id in self.state["done"]:
                self.state["done"].remove(node_id)
            # Discard uncommitted late publications and nested cursors from a
            # cancelled invocation. Previously committed results remain history.
            if node_id in batch["superseded"]:
                prefix = (*json.loads(self.scope), node_id, key)
                self.ctx._pending_publications.pop(json.dumps(prefix), None)
                for scope in list(self.ctx.cursors):
                    if tuple(json.loads(scope))[:len(prefix)] == prefix:
                        self.ctx.cursors.pop(scope, None)
                        self.ctx.node_records.pop(scope, None)

    async def _stop_replaced_loops(self):
        for node_id in list(self.state["pending"]):
            if self.state["pending_triggers"].get(node_id) != "next":
                continue
            key, batch = self._batch_for(node_id)
            if batch is None or batch["triggers"][node_id] != "loop":
                continue
            # The invalidation and result commit share the runtime commit lock.
            # Replacement intent is saved before cancellation, and survives crash.
            await self.supersede(node_id, int(key))
            task = self.tasks.get(node_id)
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._retire(key)  # a loop batch always contains exactly one node
        await self._persist_checkpoint()

    def _mark_completed(self, batch):
        state = self.state
        state["completed"] = list(dict.fromkeys(state["completed"] + batch["nodes"]))

    def _select(self, batch):
        selected = []
        transitions = []
        # Normal siblings route only after the whole batch has committed.
        records = freeze(self.ctx.node_records.get(self.scope, {}))
        with result_view(self.ctx, self.scope, records, {}):
            for node_id in batch["nodes"]:
                node = self.nodes[node_id]
                if (batch["epochs"][node_id] != self.state["epochs"].get(node_id, 0)
                        and batch["triggers"][node_id] != "loop"):
                    continue
                self.state["routed_epochs"][node_id] = batch["epochs"][node_id]
                with bind_trigger(self.ctx, batch["triggers"][node_id]):
                    targets = list(dict.fromkeys((operation, rule.node) for index, (operation, rule) in enumerate(node.routes())
                                                if self._evaluate(node_id, operation, rule.node, rule.when,
                                                                  "after_completion", batch["triggers"][node_id],
                                                                  self.ctx.node_records[self.scope][node_id]["version"], index)))
                    matched_transitions = []
                    for index, transition in enumerate(node.transition):
                        if not self._evaluate(
                            node_id,
                            "transition",
                            transition.graph,
                            transition.when,
                            "after_completion",
                            batch["triggers"][node_id],
                            self.ctx.node_records[self.scope][node_id]["version"],
                            len(node.routes()) + index,
                        ):
                            continue
                        inputs = resolve_bindings(
                            transition.inputs,
                            records=records,
                            inputs=object.__getattribute__(self.ctx, "values"),
                        )
                        matched_transitions.append(
                            (node_id, transition.graph, inputs)
                        )
                if not targets and not matched_transitions and node.default is not None:
                    targets = [("next", node.default)]
                    self.observer.event(node=node_id, operation="default", target=node.default,
                                        phase="after_completion", trigger=batch["triggers"][node_id], matched=True,
                                        snapshot_versions=dict(self.ctx.result_versions))
                selected.extend((node_id, operation, target) for operation, target in targets)
                transitions.extend(matched_transitions)
                if any(operation == "loop" for operation, _ in targets):
                    self.state["loop_sources"][node_id] = batch["triggers"][node_id]
        return selected, transitions

    def _invalidate(self, roots, *, discard_requests=False):
        state = self.state
        affected, stack = set(), list(roots)
        while stack:
            target = stack.pop()
            if target not in affected:
                affected.add(target)
                stack.extend(self.successors[target])
        for target in affected:
            state["epochs"][target] = state["epochs"].get(target, 0) + 1
        if discard_requests:
            state["waiting"] = [n for n in state["waiting"] if n not in affected]
            for target in list(state["arrivals"]):
                if target in affected:
                    del state["arrivals"][target]
                else:
                    state["arrivals"][target] = {source: count for source, count in state["arrivals"][target].items()
                                                 if source not in affected}
            for target in affected:
                if state["pending_triggers"].get(target) == "next":
                    state["pending"].remove(target)
                    state["pending_triggers"].pop(target)

    def _release_waiting(self):
        state = self.state
        for target in list(state["waiting"]):
            incoming = state["arrivals"].get(target, {})
            if _predecessors_satisfied(self.nodes[target], incoming):
                self._enqueue(target, "next")
                state["arrivals"].pop(target, None)
                state["waiting"].remove(target)

    def _route(self, selected):
        state = self.state
        jumps = list(dict.fromkeys(target for _, operation, target in selected
                                   if operation == "goto" and target.lower() not in {"end", "__end__"}))
        # A goto to the same target must not erase arrivals that, together with
        # this batch's next edges, already form a complete normal invocation.
        next_sources = {}
        for source, operation, target in selected:
            if operation == "next" and target in self.nodes:
                next_sources.setdefault(target, set()).add(source)
        complete = {
            target
            for target, sources in next_sources.items()
            if _predecessors_satisfied(
                self.nodes[target], self.state["arrivals"].get(target, {}), sources
            )
        }
        # At most one follow-up request per node. A new goto invalidates old
        # completion generations immediately, including a running old invocation.
        fresh = [target for target in jumps if target not in complete
                 and state["pending_triggers"].get(target) not in {"goto", "next", "entry"}]
        self._invalidate(fresh, discard_requests=True)
        for target in jumps:
            self._enqueue(target, "goto")
        for source, operation, target in selected:
            if target.lower() in {"end", "__end__"}:
                continue
            if operation == "loop":
                self._enqueue(target, "loop")
            elif operation == "next":
                # Coalesce repeated arrivals while a complete next request is
                # already queued. A running invocation can retain one follow-up.
                if state["pending_triggers"].get(target) == "next":
                    continue
                state["arrivals"].setdefault(target, {})[source] = 1
                if target not in state["waiting"]:
                    state["waiting"].append(target)
        self._release_waiting()

    async def _finish_stop(self):
        # terminal is durable and already blocks publication in this scope and
        # nested scopes. Mark all activations stale before cooperative cancel.
        for key, batch in list(self.state["batches"].items()):
            for node_id in batch["nodes"]:
                await self.supersede(node_id, int(key))
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for key in list(self.state["batches"]):
            self._retire(key)
        self.state["pending"].clear()
        self.state["pending_triggers"].clear()
        self.state["waiting"].clear()
        self.state["arrivals"].clear()
        self.state["loop_sources"].clear()
        self.state["finished"] = True
        await self._persist_checkpoint()
        return self.success()

    async def _finish_transition(self, source, target, inputs):
        """Commit a tail transition and discard all remaining graph work."""

        self.state["terminal"] = True
        for key, batch in list(self.state["batches"].items()):
            for node_id in batch["nodes"]:
                await self.supersede(node_id, int(key))
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for key in list(self.state["batches"]):
            self._retire(key)
        self.state["pending"].clear()
        self.state["pending_triggers"].clear()
        self.state["waiting"].clear()
        self.state["arrivals"].clear()
        self.state["loop_sources"].clear()
        self.state["finished"] = True
        object.__setattr__(self.ctx, "active_graph", target)
        object.__setattr__(self.ctx, "transition_pending", True)
        object.__setattr__(self.ctx, "transition_inputs", dict(inputs))
        self.ctx.append_history(
            {
                "graph": self.graph.id,
                "node": source,
                "step": self.ctx.step,
                "event": "transition",
                "target_graph": target,
            }
        )
        await self._persist_checkpoint()
        return GraphTransitionRequest(target, dict(inputs))

    async def _take_transition(self, transitions):
        if not transitions:
            return None
        if len(transitions) != 1:
            targets = [target for _, target, _ in transitions]
            return self.fail(
                f"multiple graph transitions selected in one wave: {targets}",
                "failed",
            )
        source, target, inputs = transitions[0]
        return await self._finish_transition(source, target, inputs)

    async def run(self):
        state = self.state
        if state["finished"]:
            return self.success()
        try:
            if state["terminal"]:
                return await self._finish_stop()
            # Resume invalidation without resurrecting cancelled self-loops.
            for key, batch in list(state["batches"].items()):
                if batch["superseded"]:
                    self._retire(key)
                else:
                    self._spawn(key, batch)
            while (state["pending"] or state["batches"]) and not state["terminal"]:
                await self._stop_replaced_loops()
                await self._launch_pending()
                unfinished = [task for task in self.tasks.values() if not task.done()]
                ready = [key for key, batch in state["batches"].items()
                         if all(n in state["done"] and (n not in self.tasks or self.tasks[n].done()) for n in batch["nodes"])]
                if not ready:
                    deadlines = [self.not_before[n] for n in state["pending"]
                                 if n not in state["wave"] and n in self.not_before]
                    timeout = max(0, min(deadlines) - time.monotonic()) if deadlines else None
                    if unfinished:
                        await asyncio.wait(unfinished, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                    elif timeout is not None:
                        await asyncio.sleep(timeout)
                if state["terminal"]:
                    return await self._finish_stop()
                # Preserve prompt failure of ordinary siblings.
                for node_id, task in list(self.tasks.items()):
                    if task.done():
                        result = task.result()
                        if result is not None:
                            return result
                ready = [key for key, batch in state["batches"].items()
                         if all(n in state["done"] and (n not in self.tasks or self.tasks[n].done())
                                for n in batch["nodes"])]
                # Predecessor batches take priority over simultaneous loop completions.
                ready.sort(key=lambda key: all(v == "loop" for v in state["batches"][key]["triggers"].values()))
                normal_ready = [key for key in ready if not all(
                    mode == "loop" for mode in state["batches"][key]["triggers"].values()
                )]
                selected = []
                transitions = []
                for key in normal_ready:
                    batch = state["batches"][key]
                    self._mark_completed(batch)
                    routes, graph_transitions = self._select(batch)
                    selected.extend(routes)
                    transitions.extend(graph_transitions)
                    self._retire(key)
                transition_result = await self._take_transition(transitions)
                if transition_result is not None:
                    return transition_result
                self._route(selected)
                # Retire old loop instances BEFORE considering their outgoing
                # routes, even when loop and predecessor finished simultaneously.
                await self._stop_replaced_loops()
                selected = []
                transitions = []
                for key in ready:
                    if key in normal_ready or key not in state["batches"]:
                        continue
                    batch = state["batches"][key]
                    self._mark_completed(batch)
                    routes, graph_transitions = self._select(batch)
                    selected.extend(routes)
                    transitions.extend(graph_transitions)
                    self._retire(key)
                transition_result = await self._take_transition(transitions)
                if transition_result is not None:
                    return transition_result
                self._route(selected)
                await self._persist_checkpoint()
            state["finished"] = True
            await self._persist_checkpoint()
            return self.success()
        except _StepLimit:
            return self.fail(f"graph exceeded max_steps={self.graph.max_steps}", "max_steps")
        except Exception as exc:
            return self.fail(str(exc), "failed")
        finally:
            tasks = list(self.tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class _StepLimit(Exception):
    pass
