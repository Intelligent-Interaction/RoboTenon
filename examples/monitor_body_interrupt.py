"""可直接运行的 Monitor -> SafetyState -> Body 中断演示。

这个例子不连接仿真器、模型或真机。executor 故意持续等待，Monitor 收到
``danger=True`` 后发布 P3；Body watchdog 读取同一个 SafetyStateStore，调用
``stop_executor``，取消当前动作并返回 ``interrupted``。

运行：

    PYTHONPATH=. python examples/monitor_body_interrupt.py
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from   robotenon.monitors.runtime import MonitorRuntime
from   robotenon.runtime.safety import SafetyStateStore
from   robotenon.schemas import Action, ActionChunk, ActionType, MonitorReport, SafetyLevel
from   robotenon.services.body import InProcessBodyService


class CollisionDemoMonitor:
    """用请求参数模拟一个碰撞检测器。"""

    async def check(self, feedback=None, *, request=None, context=None):
        danger = bool((request or {}).get("danger"))
        return MonitorReport(
            compliant=not danger,
            safety_level=SafetyLevel.P3 if danger else SafetyLevel.P0,
            source="collision-demo",
            reason="模拟碰撞" if danger else "空间安全",
        )


async def run_demo() -> None:
    safety = SafetyStateStore()
    action_started = asyncio.Event()
    stop_called = asyncio.Event()
    session_id = uuid4()

    async def executor(action: Action) -> None:
        print("[Body] 开始执行动作，预期持续 30 秒")
        action_started.set()
        try:
            await asyncio.sleep(30)
        finally:
            print("[Body executor] 收到取消，动作协程结束")

    async def stop_executor(reason: str) -> None:
        print(f"[Body stop_executor] 发送底层停止命令：{reason}")
        stop_called.set()

    body = InProcessBodyService(
        executor,
        safety_state=safety,
        poll_hz=100,
        stop_executor=stop_executor,
    )
    monitor = MonitorRuntime(
        {"collision": CollisionDemoMonitor()},
        modes={"collision": {"request"}},
        safety_state=safety,
    )
    chunk = ActionChunk(
        session_id=session_id,
        actions=[
            Action(
                session_id=session_id,
                action_type=ActionType.WAIT,
                timeout_ms=60_000,
                idempotency_key="demo-long-action",
            )
        ],
        max_duration_ms=60_000,
    )

    await body.start()
    await monitor.start()
    action_task = asyncio.create_task(body.execute(chunk))
    try:
        await asyncio.wait_for(action_started.wait(), timeout=2)
        print("[Monitor] 发布危险报告：P3")
        report = await monitor.request("collision", {"danger": True})
        await asyncio.wait_for(stop_called.wait(), timeout=2)
        result = await asyncio.wait_for(action_task, timeout=2)

        print(f"[Monitor] report={report.safety_level.value}, reason={report.reason}")
        snapshot = safety.snapshot()
        latched = any(signal.latched for signal in snapshot.signals)
        print(
            "[SafetyState] "
            f"level={snapshot.level.value}, "
            f"latched={latched}"
        )
        print(
            "[Body] "
            f"status={result.status.value}, interrupted={result.interrupted}, "
            f"error={result.error}"
        )

        assert report.safety_level is SafetyLevel.P3
        assert result.interrupted is True
        assert result.status.value == "interrupted"
    finally:
        if not action_task.done():
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
        await monitor.close()
        await body.close()


if __name__ == "__main__":
    asyncio.run(run_demo())
