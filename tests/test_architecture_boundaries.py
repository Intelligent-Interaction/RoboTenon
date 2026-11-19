"""Keep execution algorithms independent of transports and concrete stores."""

import ast
from pathlib import Path


def test_dependency_boundaries():
    root = Path(__file__).parents[1] / "robotenon"
    rules = {
        "contracts": {"workflow", "services", "storage", "modules"},
        "services": {"workflow"},
        "storage": {"workflow"},
    }
    for directory, forbidden in rules.items():
        for path in (root / directory).glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    segments = set((node.module or "").split("."))
                    assert not segments & forbidden, f"{path}: {node.module}"
    source = (root / "workflow/scheduler.py").read_text()
    assert "sqlite" not in source.lower() and "services" not in source


def test_provider_selection_stays_out_of_execution_entrypoints():
    root = Path(__file__).parents[1] / "robotenon"
    for filename in ("runtime/runner.py", "runtime/experiment.py", "runtime/worker.py"):
        tree = ast.parse((root / filename).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != "parallel", filename
            if isinstance(node, ast.Compare):
                # Routing uses declared capabilities, not concrete backend names.
                compared = [node.left, *node.comparators]
                if any(isinstance(item, ast.Attribute) and item.attr == "backend" for item in compared):
                    assert not any(isinstance(item, ast.Constant) and item.value in {"process", "in_process"}
                                   for item in compared), filename


def test_sensor_normalisation_does_not_depend_on_concrete_cache():
    root = Path(__file__).parents[1] / "robotenon"
    tree = ast.parse((root / "infra/observation_pipeline.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[-1] != "observation_cache"
