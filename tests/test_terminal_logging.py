from io import StringIO

from robotenon.runtime.runner_cli import parse_args, terminal_from_args
from robotenon.runtime.terminal import TerminalLogger, format_duration, format_rate


def test_terminal_logger_reports_single_graph_progress_without_ansi():
    stream = StringIO()
    logger = TerminalLogger(color=False, stream=stream)
    logger.start_single(
        type(
            "Request",
            (),
            {
                "config": "configs/config.json",
                "task_id": "demo",
                "env_id": None,
                "seed": 7,
                "horizon": 16,
                "graph_id": "demo_graph",
                "model_path": None,
                "dry_run": False,
            },
        )()
    )
    logger.graph_event(None, {"graph": "demo_graph", "node": "observe", "step": 1, "event": "enter"})
    logger.graph_event(
        None,
        {
            "graph": "demo_graph",
            "node": "observe",
            "step": 1,
            "event": "exit",
            "status": "completed",
            "success": True,
        },
    )

    output = stream.getvalue()
    assert "single episode" in output
    assert "NODE" in output
    assert "demo_graph/observe" in output
    assert "\033[" not in output


def test_terminal_flags_keep_logs_optional_and_machine_output_unchanged():
    args = parse_args(["--batch", "--quiet", "--no-color", "--verbose"])
    logger = terminal_from_args(args)
    assert logger.enabled is False
    assert logger.color is False
    assert logger.verbose is True


def test_terminal_formatters_are_stable_for_progress_lines():
    assert format_duration(62.3) == "1m02.3s"
    assert format_duration(None) == "-"
    assert format_rate(3, 4) == "3/4 (75.0%)"
    assert format_rate(0, 0) == "0/0 (-)"
