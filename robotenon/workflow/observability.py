"""Bounded routing diagnostics and best-effort existing Trace/Metrics producers."""
import json


class RoutingObserver:
    LIMIT = 200

    def __init__(self, ctx, graph, scope, state):
        self.ctx, self.graph, self.scope, self.state = ctx, graph, scope, state
        state.setdefault('routing_events', [])
        state.setdefault('routing_events_dropped', 0)
        state.setdefault('loop_metrics', {})

    def event(self, **event):
        event = dict(graph=self.graph.id, scope=json.loads(self.scope), **event)
        events = self.state['routing_events']
        events.append(event)
        if len(events) > self.LIMIT:
            self.state['routing_events_dropped'] += len(events) - self.LIMIT
            del events[:-self.LIMIT]
        try:
            if self.ctx.trace is not None:
                with self.ctx.trace.span('routing:' + event['node'], attributes=dict(kind='routing', **event)):
                    pass
        except Exception:
            # Observability must not change control behavior.
            pass

    def timing(self, node, duration_ms, interval_ms, skipped, trigger, activation, status):
        if node.loop is None:
            return
        stats = self.state['loop_metrics'].setdefault(node.id, {
            'invocations': 0, 'loop_invocations': 0, 'skipped_ticks': 0,
            'duration_total_ms': 0.0, 'interval_samples': 0, 'interval_total_ms': 0.0,
        })
        stats['invocations'] += 1
        stats['loop_invocations'] += int(trigger == 'loop')
        stats['skipped_ticks'] += skipped
        stats['duration_total_ms'] += duration_ms
        stats['last_duration_ms'] = duration_ms
        stats['last_status'] = status
        stats['target_interval_ms'] = node.loop.interval_seconds() * 1000
        if interval_ms is not None:
            stats['last_interval_ms'] = interval_ms
            stats['interval_total_ms'] += interval_ms
            stats['interval_samples'] += 1
        self.event(node=node.id, phase='execution_timing', trigger=trigger, activation=activation,
                   duration_ms=duration_ms, interval_ms=interval_ms, skipped_ticks=skipped, status=status)
        try:
            metrics = self.ctx.metrics
            if metrics is not None:
                fields = {'labels': {'graph': self.graph.id, 'node': node.id}, 'session_id': self.ctx.session_id}
                metrics.histogram('workflow.loop.duration_ms', duration_ms, unit='ms', **fields)
                if interval_ms is not None:
                    metrics.histogram('workflow.loop.interval_ms', interval_ms, unit='ms', **fields)
                metrics.counter('workflow.loop.skipped_ticks', skipped, **fields)
                metrics.gauge('workflow.loop.target_interval_ms', stats['target_interval_ms'], unit='ms', **fields)
        except Exception:
            pass
