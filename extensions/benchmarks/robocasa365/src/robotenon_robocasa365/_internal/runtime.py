"""Compatibility Tool name; new Graphs use the shared backend runtime."""
from robotenon_benchmark_support.runtime import BackendRuntime
from robotenon.tools.registry import register_tool


@register_tool("xiaomi_native_batch", kind="model")
class XiaomiNativeBatch(BackendRuntime):
    def __init__(self, *, obs_history=4, obs_interval=2, replan_steps=16, **kwargs):
        super().__init__(declaration={"history": {"length": obs_history,
            "interval": obs_interval}, "replan_steps": replan_steps}, **kwargs)
