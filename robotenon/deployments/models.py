"""Launch recipes for bundled model integrations."""


def _command(config, host, port, backend):
    if not config.model_path:
        raise ValueError("managed model deployment requires model_path")
    reserved = {"--host", "--port", "--model", "--device", "--tensor-parallel-size"}
    if any(arg.split("=", 1)[0] in reserved for arg in config.extra_args):
        raise ValueError("extra_args cannot override model, endpoint or GPU placement")
    if backend == "vllm":
        command = [
            config.executable or "vllm",
            "serve",
            config.model_path,
            "--tensor-parallel-size",
            str(len(config.gpu_ids)),
            "--distributed-executor-backend",
            "mp",
        ]
    else:
        command = [
            config.executable or "transformers",
            "serve",
            config.model_path,
            "--device",
            "cuda:0" if len(config.gpu_ids) == 1 else "balanced",
            "--dtype",
            "bfloat16",
            "--attn-implementation",
            "sdpa",
            "--reasoning",
            "off",
        ]
    return [*command, "--host", host, "--port", str(port), *config.extra_args]


def vllm(config, host, port):
    return _command(config, host, port, "vllm")


def transformers(config, host, port):
    return _command(config, host, port, "transformers")
