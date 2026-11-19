"""Regression test for the visible Monitor-to-Body interruption example."""

import asyncio

from examples.monitor_body_interrupt import run_demo


def test_monitor_body_interrupt_demo_runs_to_interrupted_result(capsys):
    asyncio.run(run_demo())
    output = capsys.readouterr().out
    assert "[Monitor] report=P3" in output
    assert "[Body stop_executor]" in output
    assert "[Body] status=interrupted, interrupted=True" in output
