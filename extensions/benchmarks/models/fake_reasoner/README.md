# Fake planner / verifier

[**English**](README.md) · [简体中文](README.zh-CN.md)

This deterministic test provider exercises system graph routing and lifecycles. Use a real model to assess planning or verification quality.

Install in the model environment:

```bash
python -m pip install -e extensions/benchmarks/models/fake_reasoner
```

The framework service factory is `robotenon_fake_reasoner.backend:Provider`.
Set `role` to `planner` or `verifier`. The planner returns the original task as the subgoal. The verifier always returns subtask_success=false. Responses intentionally arrive in reverse order to test request_id routing.

The package requires no assets, weights, or simulator libraries. See the [RoboCasa hierarchical system](../../systems/robocasa_hierarchical/README.md) for a runnable example.
