import asyncio

from examples.monitor_body_environment_demo import run_demo


def test_environment_continues_after_body_interruption():
    result = asyncio.run(run_demo(render=False, post_stop_seconds=0.25))
    assert result["safety_level"] == "P3"
    assert result["body_status"] == "interrupted"
    assert result["environment_ticks_after_stop"] >= 3
    assert result["robot_delta_after_stop"] == 0.0
