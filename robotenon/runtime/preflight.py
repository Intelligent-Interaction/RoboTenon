"""Shared bounded, read-only startup diagnostics for the CLI and Portal."""

import ipaddress
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .execution import ExecutionOptions


def check_preflight(
    loaded,
    *,
    output_root,
    probe_gpu=True,
    graph_id=None,
    task_adapter_overrides=None,
    experiment_request=None,
    batch=False,
    output_file=None,
):
    checks = []

    def add(name, status, detail):
        checks.append({"name": name, "status": status, "detail": str(detail)})

    try:
        execution = ExecutionOptions.from_params(loaded.params)
        execution.validate_tools(loaded.tools)
        from .result_projection import require_episode_outputs
        from .runner import EpisodeRequest, _graph_selector

        graph = _graph_selector(
            loaded,
            loaded.params,
            EpisodeRequest(config=loaded.bundle.config, graph_id=graph_id),
        )
        require_episode_outputs(loaded.graphs.graph_map()[graph])
        add(
            "config",
            "ok",
            "configuration, graph outputs and Worker allocation are valid",
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary reports plugin validation failures
        add("config", "error", exc)
        return {
            "ok": False,
            "checks": checks,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    if batch:
        try:
            from .experiment import ExperimentRequest, prepare_task_adapters

            request = experiment_request
            if request is None and task_adapter_overrides is not None:
                request = ExperimentRequest(
                    config=loaded.bundle.config,
                    task_adapter_overrides=task_adapter_overrides,
                )
            adapters = prepare_task_adapters(loaded, request)
            add(
                "task_adapters",
                "ok",
                "adapter plugins, effective overrides and constructors are valid: "
                + ", ".join(item.instance_id for item in adapters),
            )
        except Exception as exc:  # noqa: BLE001 - report adapter validation failures
            add("task_adapters", "error", exc)

    requested_root = Path(output_root).expanduser().resolve()
    root = requested_root
    invalid_component = None
    while True:
        if root.exists():
            if root.is_dir():
                break
            invalid_component = root
            root = root.parent
            while not root.exists() and root != root.parent:
                root = root.parent
            break
        if root == root.parent:
            break
        root = root.parent
    if invalid_component is not None:
        add(
            "output",
            "error",
            f"output path contains a file where a directory is required: {invalid_component}",
        )
    elif root.is_dir():
        writable = os.access(root, os.W_OK | os.X_OK)
        add(
            "output",
            "ok" if writable else "error",
            f"output directory path: {requested_root}; existing ancestor: {root}",
        )
    else:
        add("output", "error", f"no existing directory ancestor for {requested_root}")
    if output_file is not None:
        candidate = Path(output_file).expanduser().resolve()
        if candidate.exists() and not candidate.is_file():
            add(
                "output_file",
                "error",
                f"output file path is not a regular file: {candidate}",
            )
        elif candidate.exists() and not os.access(candidate, os.W_OK):
            add("output_file", "error", f"output file is not writable: {candidate}")
        else:
            add("output_file", "ok", f"output file target: {candidate}")
    free = shutil.disk_usage(root).free if root.is_dir() else 0
    add(
        "disk",
        "ok" if free >= 1024**3 else "warning" if root.is_dir() else "error",
        f"{free} bytes free at {root}; recommended minimum 1 GiB",
    )
    for tool in loaded.tools.tools:
        if not tool.enabled or tool.deployment is None:
            continue
        deployment, name = tool.deployment, tool.id or tool.name
        model = deployment.model_path
        if model:
            path = Path(model).expanduser()
            local = path.is_absolute() or str(model).startswith(("./", "../", "~"))
            add(
                f"{name}.model",
                "error"
                if local and not path.exists()
                else ("ok" if local else "deferred"),
                f"local model: {path}"
                if local
                else f"model identifier {model}; availability checked by model service",
            )
        executable = deployment.executable or (
            deployment.command[0]
            if deployment.backend == "command" and deployment.command
            else None
        )
        if executable:
            add(
                f"{name}.executable",
                "ok" if shutil.which(executable) else "error",
                executable,
            )
        add(
            f"{name}.readiness",
            "deferred",
            f"Worker waits for {deployment.health_path or 'TCP connection'}; timeout {deployment.startup_timeout_s}s",
        )
    model = loaded.params.resolve("model.path")
    if model:
        path = Path(str(model)).expanduser()
        local = path.is_absolute() or str(model).startswith(("./", "../", "~"))
        add(
            "model.path",
            "error" if local and not path.exists() else ("ok" if local else "deferred"),
            model,
        )
    host, port = (
        loaded.params.resolve("model.host"),
        loaded.params.resolve("model.port"),
    )
    numeric_port = None
    if port is not None:
        try:
            parsed_port = int(port)
            if isinstance(port, bool) or (
                not isinstance(port, str) and port != parsed_port
            ):
                raise ValueError("model.port must be an integer")
            numeric_port = parsed_port
        except (TypeError, ValueError) as exc:
            add("model.endpoint", "error", f"invalid model.port {port!r}: {exc}")
        if numeric_port is not None and not 1 <= numeric_port <= 65535:
            add("model.endpoint", "error", "model.port must be 1..65535")
            numeric_port = None
    if (
        host
        and numeric_port is not None
        and not any(tool.enabled and tool.deployment for tool in loaded.tools.tools)
    ):
        try:
            address = (
                "127.0.0.1"
                if host == "localhost"
                else str(ipaddress.ip_address(str(host)))
            )
        except ValueError as exc:
            add(
                "model.endpoint",
                "deferred",
                f"{host}:{numeric_port}: {exc}; DNS endpoints are checked by the Worker",
            )
        else:
            try:
                with socket.create_connection((address, numeric_port), timeout=0.25):
                    pass
                add(
                    "model.endpoint",
                    "ok",
                    f"TCP endpoint {host}:{numeric_port} accepts connections; model readiness is checked at execution",
                )
            except OSError as exc:
                add(
                    "model.endpoint",
                    "warning",
                    f"{host}:{numeric_port} is not ready: {exc}",
                )
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
        add(
            "ports",
            "ok",
            "local dynamic port allocation available; exclusive reservation occurs at service startup",
        )
    except OSError as exc:
        add("ports", "error", exc)
    if execution.gpu_ids:
        if probe_gpu and shutil.which("nvidia-smi"):
            try:
                proc = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=index,uuid,memory.free",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=True,
                )
                devices = [line.split(",") for line in proc.stdout.splitlines()]
                known = {part.strip() for row in devices for part in row[:2]}
                missing = set(execution.gpu_ids) - known
                # MIG identifiers need a device-specific runtime check.
                missing = {value for value in missing if not value.startswith("MIG-")}
                add(
                    "gpu",
                    "error" if missing else "ok",
                    f"missing: {sorted(missing)}; devices (index, UUID, free MiB): {proc.stdout[:4000]}",
                )
            except (OSError, subprocess.SubprocessError) as exc:
                add("gpu", "warning", f"GPU probe failed: {exc}")
        else:
            add(
                "gpu",
                "deferred",
                f"GPU probe unavailable; Worker must validate {execution.gpu_ids}",
            )
    else:
        add("gpu", "ok", "no GPU allocation requested")
    return {
        "ok": not any(item["status"] == "error" for item in checks),
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
