# Pinned reference

[**English**](README.md) · [简体中文](README.zh-CN.md)

Unmodified `src/vla_eval/runners/sync_runner.py` from allenai/vla-evaluation-harness
commit `6cc3e1bd9fc4bed26fb0a3aabd342c5fed11ca09`, Apache-2.0 (see LICENSE).
SHA256: `cf6234c83d8599f49dc8f212d4f8225967a71a937e8ff3e1dbc0873b6931d197`.

The parity test executes the original method AST without importing the upstream
package. Only class inheritance and annotation imports are detached. It compares
that method with RoboTenon Graph execution using the same deterministic fixture.
