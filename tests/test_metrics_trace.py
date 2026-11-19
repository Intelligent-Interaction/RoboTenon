import asyncio
import json
import threading
import time

import math

import pytest

from robotenon.infra.metrics import MetricDefinition, MetricsClient, MetricsDaemon, histogram_quantile
from robotenon.infra.trace import TraceClient, TraceDaemon
from robotenon.modules.base import ModuleRuntime
from robotenon.schemas import TraceSpan
from robotenon.tools import ToolResult, ToolRuntime
from robotenon.workflow import GraphContext, GraphRuntime
from robotenon.workflow.definition import GraphDefinition


def test_metrics_and_trace_are_independent(tmp_path):
    async def scenario():
        metrics = MetricsDaemon(tmp_path / "metrics.jsonl")
        metrics.registry.register(MetricDefinition("episode_total", "counter", allowed_labels=frozenset()))
        trace = TraceDaemon(tmp_path / "trace.jsonl")
        mc = MetricsClient(metrics)
        tc = TraceClient(trace)
        mc.counter("episode_total")
        with tc.span("unit") as span:
            span.set_attribute("component", "test")
            span.add_event("finished")
        assert await mc.flush() == 1
        assert await tc.flush() == 1
        assert metrics.counter_value("episode_total") == 1
        assert len(trace.snapshot()) == 1
        assert trace.snapshot()[0].attributes["component"] == "test"
        assert (tmp_path / "metrics.jsonl").exists()
        assert (tmp_path / "trace.jsonl").exists()

    asyncio.run(scenario())


def test_open_trace_span_is_queryable_before_node_finishes(tmp_path):
    async def scenario():
        daemon = TraceDaemon(tmp_path / "trace.jsonl")
        trace = TraceClient(daemon, flush_interval=0.01)
        await trace.start()
        with trace.span("long-running"):
            await asyncio.sleep(0.04)
            rows = daemon.sql("SELECT name, end_time, status FROM spans WHERE name='long-running'")
            assert rows == [{"name": "long-running", "end_time": None, "status": "unset"}]
        await trace.close()
        rows = daemon.sql("SELECT name, end_time, status FROM spans WHERE name='long-running'")
        assert rows[0]["end_time"] is not None
        assert rows[0]["status"] == "ok"
        daemon.close()

    asyncio.run(scenario())


def test_histogram_buckets_exposition_and_reusable_quantiles(tmp_path):
    metrics = MetricsDaemon(tmp_path / "metrics.jsonl")
    labels = {"environment": "lab-a", "type": "pick"}
    for value in (1.0, 5.0, 10.0, 15.0, 20.0):
        metrics.ingest({"name": "latency_ms", "value": value, "kind": "histogram", "labels": labels,
                        "histogram_buckets": (5.0, 10.0, 20.0)})

    text = metrics.prometheus_text()
    assert "# TYPE latency_ms histogram" in text
    assert 'latency_ms_bucket{environment="lab-a",le="5",type="pick"} 2' in text
    assert 'latency_ms_bucket{environment="lab-a",le="10",type="pick"} 3' in text
    assert 'latency_ms_bucket{environment="lab-a",le="+Inf",type="pick"} 5' in text
    assert 'latency_ms_sum{environment="lab-a",type="pick"} 51.0' in text
    assert 'latency_ms_count{environment="lab-a",type="pick"} 5' in text
    assert metrics.quantile("latency_ms", 0.50, labels=labels) == pytest.approx(7.5)
    assert metrics.quantile("latency_ms", 0.95, labels=labels) == pytest.approx(18.75)
    assert histogram_quantile(0.90, {5.0: 2, 10.0: 3, 20.0: 5, math.inf: 5}) == pytest.approx(17.5)
    assert metrics.quantile("missing", 0.95) is None
    with pytest.raises(ValueError):
        histogram_quantile(1.1, {1.0: 1, math.inf: 1})


def test_configured_histogram_layout_is_persisted_in_wal(tmp_path):
    path = tmp_path / "metrics.jsonl"
    metrics = MetricsDaemon(path, histogram_buckets={"temperature": (-10, 0, 10)})
    metrics.ingest({"name": "temperature", "value": 4, "kind": "histogram"})
    raw = path.read_text()
    assert json.loads(raw)["histogram_buckets"] == [-10.0, 0.0, 10.0]

    restored = MetricsDaemon(path)
    text = restored.prometheus_text()
    assert 'temperature_bucket{le="-10"} 0' in text
    assert 'temperature_bucket{le="10"} 1' in text


def test_metrics_and_trace_reload_incrementally(tmp_path):
    metrics_path = tmp_path / "metrics.jsonl"
    trace_path = tmp_path / "trace.jsonl"
    writer_metrics = MetricsDaemon(metrics_path)
    reader_metrics = MetricsDaemon(metrics_path)
    writer_metrics.ingest({"name": "ticks", "value": 1, "kind": "counter"})
    assert len(reader_metrics.snapshot(name="ticks")) == 1
    writer_metrics.ingest({"name": "ticks", "value": 1, "kind": "counter"})
    assert len(reader_metrics.snapshot(name="ticks")) == 2
    assert reader_metrics.counter_value("ticks") == 2

    writer_trace = TraceDaemon(trace_path)
    reader_trace = TraceDaemon(trace_path)
    writer_trace.ingest({"name": "first"})
    assert len(reader_trace.snapshot()) == 1
    writer_trace.ingest({"name": "second"})
    assert [span.name for span in reader_trace.snapshot()] == ["first", "second"]


def test_trace_sql_rejects_mutating_pragma(tmp_path):
    trace = TraceDaemon(tmp_path / "trace.jsonl")
    assert trace.sql("PRAGMA table_info(spans)")
    with pytest.raises(ValueError):
        trace.sql("PRAGMA user_version=1")
    with pytest.raises(ValueError):
        trace.sql("PRAGMA journal_mode=OFF")


def test_loop_trace_keeps_each_node_call_and_tool_result_correlated(tmp_path):
    class EchoTool:
        async def call(self, arguments, context):
            return ToolResult(output={"count": int(arguments["count"])})

    class LoopModule:
        async def run(self, ctx):
            count = int(ctx.previous.get("count", 0)) + 1
            ctx.values["count"] = count
            result = await ctx.call_tool("echo", {"count": count})
            return {"count": count, "tool": result.output}

    graph = GraphDefinition.model_validate(
        {
            "id": "trace-loop",
            "entry": "loop",
            "backend": "fallback",
            "max_steps": 10,
            "nodes": [
                {'id': 'loop', 'module': 'trace-loop-module', 'default': 'END', 'predecessors': [], 'loop': {'when': "ctx.results['loop']['count'] < 3"}}
            ],
        }
    )

    async def scenario():
        daemon = TraceDaemon(tmp_path / "trace.jsonl")
        trace = TraceClient(daemon)
        tools = ToolRuntime({"echo": EchoTool()})
        modules = ModuleRuntime({"trace-loop-module": LoopModule()}, tools=tools)
        runtime = GraphRuntime(
            {graph.id: graph},
            modules,
            tools=tools,
            backend="fallback",
            trace=trace,
        )
        result = await runtime.run("trace-loop", ctx=GraphContext())
        await trace.close()
        await modules.close()
        await tools.close()
        spans = daemon.snapshot()
        daemon.close()
        return result, spans

    result, spans = asyncio.run(scenario())
    assert result.success is True
    assert result.context.results["loop"]["count"] == 3

    nodes = sorted(
        (span for span in spans if span.name == "node:trace-loop:loop"),
        key=lambda span: span.attributes["step_index"],
    )
    tools = sorted(
        (span for span in spans if span.name == "tool:echo"),
        key=lambda span: span.attributes["caller_step_index"],
    )
    assert len(nodes) == len(tools) == 3
    assert [span.attributes["step_index"] for span in nodes] == [1, 2, 3]
    assert [span.attributes["iteration"] for span in nodes] == [1, 2, 3]
    for node, tool in zip(nodes, tools):
        assert tool.parent_span_id == node.span_id
        assert tool.attributes["caller_node"] == "loop"
        assert tool.attributes["caller_step_index"] == node.attributes["step_index"]
        assert tool.attributes["caller_iteration"] == node.attributes["iteration"]
        assert tool.attributes["result"] == {"count": node.attributes["step_index"]}


def test_custom_module_can_emit_metrics_and_nested_trace(tmp_path):
    class InstrumentedModule:
        async def run(self, ctx):
            assert ctx.metrics is not None
            assert ctx.trace is not None
            ctx.metrics.counter(
                "custom_module_calls_total",
                labels={"component": "instrumented"},
                scope="episode",
            )
            with ctx.trace.span(
                "custom.module.work",
                attributes={"component": "instrumented", "task": ctx.task_id},
            ) as span:
                span.add_event("checkpoint", {"step": ctx.step})
            return {"instrumented": True}

    graph = GraphDefinition.model_validate(
        {
            "id": "custom-observability",
            "entry": "instrumented",
            "backend": "fallback",
            "nodes": [{"id": "instrumented", "module": "instrumented-module"}],
        }
    )

    async def scenario():
        metrics_daemon = MetricsDaemon(tmp_path / "metrics.jsonl")
        metrics = MetricsClient(metrics_daemon)
        trace_daemon = TraceDaemon(tmp_path / "trace.jsonl")
        trace = TraceClient(trace_daemon)
        modules = ModuleRuntime({"instrumented-module": InstrumentedModule()})
        runtime = GraphRuntime(
            {graph.id: graph},
            modules,
            backend="fallback",
            metrics=metrics,
            trace=trace,
        )
        result = await runtime.run(
            "custom-observability",
            ctx=GraphContext(task_id="task-observe"),
        )
        await metrics.close()
        await trace.close()
        await modules.close()
        return result, metrics_daemon, trace_daemon

    result, metrics_daemon, trace_daemon = asyncio.run(scenario())
    assert result.success is True
    assert metrics_daemon.counter_value(
        "custom_module_calls_total", {"component": "instrumented"}
    ) == 1
    custom_spans = [
        span for span in trace_daemon.snapshot() if span.name == "custom.module.work"
    ]
    assert len(custom_spans) == 1
    assert custom_spans[0].attributes["component"] == "instrumented"
    assert custom_spans[0].attributes["events"][0]["name"] == "checkpoint"
    assert "metrics" not in result.context.to_dict()
    assert "trace" not in result.context.to_dict()
    trace_daemon.close()


def test_metrics_reject_invalid_prometheus_label_names(tmp_path):
    metrics = MetricsDaemon(tmp_path / "metrics.jsonl")
    with pytest.raises(ValueError, match="label names"):
        metrics.ingest(
            {"name": "latency", "value": 1, "kind": "gauge", "labels": {"bad.name": "x"}}
        )


def test_metric_and_trace_emit_are_thread_safe_and_sync_sinks_are_offloaded():
    async def scenario():
        metric_batches = []
        trace_batches = []

        def metric_sink(batch):
            time.sleep(0.05)
            metric_batches.append(batch)

        def trace_sink(batch):
            time.sleep(0.05)
            trace_batches.append(batch)

        metrics = MetricsClient(metric_sink, capacity=128)
        traces = TraceClient(trace_sink, capacity=128)
        def emit_many():
            for _ in range(20):
                metrics.counter("threaded")

        threads = [threading.Thread(target=emit_many) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(metrics.buffer) == 80
        traces.emit(TraceSpan(name="threaded"))

        started = time.perf_counter()
        await asyncio.gather(metrics.flush(), traces.flush())
        # The synchronous sinks slept in worker threads, so both flushes can
        # overlap and the event loop stays responsive.
        assert time.perf_counter() - started < 0.2
        assert len(metric_batches) == 1 and len(metric_batches[0]) == 80
        assert len(trace_batches) == 1 and len(trace_batches[0]) == 1

    asyncio.run(scenario())


def test_replaced_episode_trace_is_deleted_without_replay_or_late_resurrection(tmp_path):
    wal = tmp_path / 'trace.jsonl'
    store = TraceDaemon(wal)
    old = TraceSpan(name='old reset', run_id='run', episode_id='one')
    other = TraceSpan(name='keep', run_id='run', episode_id='two')
    store.ingest_batch([old, other])
    store.delete_episode('run', 'one')
    store.ingest(old)  # A buffered old worker span must not resurrect it.
    new = TraceSpan(name='new reset', run_id='run', episode_id='one')
    store.ingest(new)
    assert {s.name for s in store.snapshot()} == {'keep', 'new reset'}
    store.close()
    reopened = TraceDaemon(wal)
    assert {s.name for s in reopened.snapshot()} == {'keep', 'new reset'}
    assert 'old reset' not in wal.read_text()
    reopened.close()
