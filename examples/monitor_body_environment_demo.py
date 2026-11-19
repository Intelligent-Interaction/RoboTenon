"""Environment-level Monitor/Body interruption demo with a visible timeline.

The environment loop keeps advancing an obstacle and its clock after the Body
has stopped the robot.  If Pillow is installed, the script also writes a GIF
and a before/after PNG.  The terminal output is always available.

Run from the repository root:

    PYTHONPATH=. python examples/monitor_body_environment_demo.py
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from robotenon.monitors.runtime import MonitorRuntime
from robotenon.runtime.safety import SafetyStateStore
from robotenon.schemas import Action, ActionChunk, ActionType, MonitorReport, SafetyLevel
from robotenon.services.body import InProcessBodyService


@dataclass(frozen=True)
class WorldFrame:
    tick: int
    robot_x: float
    obstacle_x: float
    robot_moving: bool
    safety_level: str


@dataclass
class DemoWorld:
    """A tiny environment with an independently advancing simulation clock."""

    dt: float = 0.05
    robot_x: float = 70.0
    obstacle_x: float = 520.0
    robot_speed: float = 120.0
    tick: int = 0
    robot_moving: bool = False
    safety_level: str = "P0"
    frames: list[WorldFrame] = field(default_factory=list)

    def advance(self) -> None:
        self.tick += 1
        if self.robot_moving:
            self.robot_x = min(560.0, self.robot_x + self.robot_speed * self.dt)
        # This represents the environment's own dynamics. It keeps changing
        # even after the Body has set robot_moving=False.
        self.obstacle_x -= 5.0
        if self.obstacle_x < 40.0:
            self.obstacle_x = 520.0
        self.frames.append(
            WorldFrame(
                tick=self.tick,
                robot_x=self.robot_x,
                obstacle_x=self.obstacle_x,
                robot_moving=self.robot_moving,
                safety_level=self.safety_level,
            )
        )

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self.advance()
            await asyncio.sleep(self.dt)


class CollisionMonitor:
    async def check(self, feedback=None, *, request=None, context=None):
        danger = bool((request or {}).get("danger"))
        return MonitorReport(
            compliant=not danger,
            safety_level=SafetyLevel.P3 if danger else SafetyLevel.P0,
            source="collision-monitor",
            reason="obstacle entered safety zone" if danger else "clear",
        )


def _draw_scene(draw, frame: WorldFrame, *, offset: int, title: str) -> None:
    left, top, width, height = offset + 20, 55, 390, 150
    draw.text((offset + 20, 15), title, fill="black")
    draw.rectangle((left, top, left + width, top + height), outline="#555555", width=2)
    draw.line((left, top + 120, left + width, top + 120), fill="#999999", width=2)
    robot_color = "#238636" if frame.robot_moving else "#d73a49"
    robot_x = left + int(frame.robot_x * 0.62)
    obstacle_x = left + int(frame.obstacle_x * 0.62)
    draw.rectangle((robot_x, top + 92, robot_x + 28, top + 120), fill=robot_color)
    draw.ellipse((robot_x + 5, top + 76, robot_x + 23, top + 94), fill=robot_color)
    draw.rectangle((obstacle_x, top + 98, obstacle_x + 26, top + 120), fill="#f0883e")
    draw.text(
        (offset + 20, 220),
        f"env_tick={frame.tick}  robot={'RUN' if frame.robot_moving else 'STOP'}  safety={frame.safety_level}",
        fill="black",
    )


def render_frames(frames: list[WorldFrame], stop_tick: int, gif_path: Path, png_path: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[render] Pillow 未安装，跳过 GIF/PNG；终端时间线仍然有效")
        return

    gif_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the animation short while retaining the pre-stop and post-stop
    # phases. Every frame is an environment observation, not a Body callback.
    sampled = frames[::2] or frames
    images = []
    for frame in sampled:
        image = Image.new("RGB", (860, 260), "white")
        draw = ImageDraw.Draw(image)
        _draw_scene(draw, frame, offset=0, title="robot before/after Monitor P3")
        images.append(image)
    images[0].save(
        gif_path,
        save_all=True,
        append_images=images[1:],
        duration=100,
        loop=0,
    )

    before = next(
        (
            item
            for item in frames
            if item.tick >= stop_tick and not item.robot_moving
        ),
        frames[-1],
    )
    after = frames[-1]
    image = Image.new("RGB", (860, 260), "white")
    draw = ImageDraw.Draw(image)
    _draw_scene(draw, before, offset=0, title="after Monitor P3: body stopped")
    _draw_scene(draw, after, offset=430, title="environment continued +3s")
    image.save(png_path)
    print(f"[render] GIF: {gif_path}")
    print(f"[render] PNG: {png_path}")


async def run_demo(
    *,
    render: bool = True,
    gif_path: str | Path = "runs/monitor-body-environment-demo.gif",
    png_path: str | Path = "runs/monitor-body-environment-demo.png",
    post_stop_seconds: float = 3.0,
) -> dict[str, float | int | str]:
    if post_stop_seconds <= 0:
        raise ValueError("post_stop_seconds must be positive")
    world = DemoWorld()
    safety = SafetyStateStore()
    environment_stop = asyncio.Event()
    action_started = asyncio.Event()
    stop_called = asyncio.Event()
    session_id = uuid4()
    stop_tick = 0
    stop_robot_x = 0.0

    async def executor(action: Action) -> None:
        world.robot_moving = True
        action_started.set()
        print("[Body] robot starts moving inside environment")
        try:
            await asyncio.Event().wait()
        finally:
            print("[Body executor] action coroutine cancelled")

    async def stop_executor(reason: str) -> None:
        nonlocal stop_tick, stop_robot_x
        world.robot_moving = False
        world.safety_level = "P3"
        stop_tick = world.tick
        stop_robot_x = world.robot_x
        stop_called.set()
        print(f"[Body stop_executor] robot STOP/HOLD: {reason}")

    body = InProcessBodyService(
        executor,
        safety_state=safety,
        poll_hz=100,
        stop_executor=stop_executor,
    )
    monitor = MonitorRuntime(
        {"collision": CollisionMonitor()},
        modes={"collision": {"request"}},
        safety_state=safety,
    )
    action = Action(
        session_id=session_id,
        action_type=ActionType.CUSTOM,
        idempotency_key="environment-demo-motion",
    )
    chunk = ActionChunk(session_id=session_id, actions=[action], max_duration_ms=60_000)
    environment_task = asyncio.create_task(world.run(environment_stop))
    action_task = asyncio.create_task(body.execute(chunk))
    await body.start()
    await monitor.start()
    try:
        await asyncio.wait_for(action_started.wait(), timeout=2)
        await asyncio.sleep(0.35)
        print("[Monitor] obstacle enters safety zone -> publish P3")
        report = await monitor.request("collision", {"danger": True})
        world.safety_level = report.safety_level.value
        await asyncio.wait_for(stop_called.wait(), timeout=2)
        result = await asyncio.wait_for(action_task, timeout=2)
        robot_at_result = world.robot_x
        # Keep the environment running long enough to make the separation
        # visible in the GIF and in the tick/position counters.
        await asyncio.sleep(post_stop_seconds)
        environment_stop.set()
        await environment_task

        snapshot = safety.snapshot()
        environment_ticks_after_stop = world.tick - stop_tick
        robot_delta_after_stop = abs(world.robot_x - stop_robot_x)
        print(
            f"[Environment] tick {stop_tick} -> {world.tick} "
            f"(continued {environment_ticks_after_stop} ticks), "
            f"obstacle_x={world.obstacle_x:.1f}"
        )
        print(
            f"[Body] status={result.status.value}, robot_x={robot_at_result:.1f} "
            f"-> {world.robot_x:.1f}, delta_after_stop={robot_delta_after_stop:.1f}"
        )
        print(
            f"[SafetyState] level={snapshot.level.value}, "
            f"latched={any(signal.latched for signal in snapshot.signals)}"
        )
        assert result.interrupted is True
        assert result.status.value == "interrupted"
        assert environment_ticks_after_stop >= 3
        assert robot_delta_after_stop == 0.0
        if render:
            render_frames(world.frames, stop_tick, Path(gif_path), Path(png_path))
        return {
            "body_status": result.status.value,
            "safety_level": snapshot.level.value,
            "stop_tick": stop_tick,
            "final_tick": world.tick,
            "environment_ticks_after_stop": environment_ticks_after_stop,
            "robot_delta_after_stop": robot_delta_after_stop,
        }
    finally:
        environment_stop.set()
        if not environment_task.done():
            await environment_task
        if not action_task.done():
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
        await monitor.close()
        await body.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="show environment continuing after Body interruption")
    parser.add_argument("--gif", default="runs/monitor-body-environment-demo.gif")
    parser.add_argument("--png", default="runs/monitor-body-environment-demo.png")
    parser.add_argument(
        "--post-stop-seconds",
        type=float,
        default=3.0,
        help="how long the environment keeps running after Body interruption",
    )
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        run_demo(
            render=not args.no_render,
            gif_path=args.gif,
            png_path=args.png,
            post_stop_seconds=args.post_stop_seconds,
        )
    )


if __name__ == "__main__":
    main()
