# Benchmark setup documentation verification — 2026-09-29

[**English**](benchmark-setup-docs-2026-09-29.md) · [简体中文](benchmark-setup-docs-2026-09-29.zh-CN.md)

## Changes

- Added a shared installation/result-check guide and 20 benchmark-specific SETUP.md guides linked from each README.
- Added runtime versions, upstream pins, asset sources/download commands where available, required path configuration, fake-action commands, real-backend migration and verification limits.
- Linked RoboDojo Office to its simulator prerequisites while preserving its specialized entrypoint.
- Corrected Simpler's OpenCV pin from 4.12.0.88 to 4.11.0.86. PyPI metadata confirms 4.12 requires NumPy >=2 for Python >=3.9, conflicting with the benchmark's NumPy 1.24.4. The 4.11 requirement is compatible on the documented Python 3.10 stack.

## Checks performed

- 37 passed: `test_native_graph_configs.py`, `test_unified_benchmark_backend.py`, `test_live_benchmark.py` (benchmark-test-env, Python 3.11; all benchmark src directories on test PYTHONPATH).
- All 157 catalog profiles generated and loaded through `application_config`, `write_application_config`, `load_application_config`.
- 21 setup documents / 65 bash blocks checked with `bash -n`; relative links resolved without errors.
- Confirmed the pinned server reference contains CALVIN validation `.hydra`/statistics and LIBERO-Mem pre-generated initial states.
- Checked ManiSkill2 `MS2_ASSET_DIR` and Simpler `MS2_REAL2SIM_ASSET_DIR` against their pinned source implementations.
- Checked OpenCV dependency metadata directly on PyPI.

These checks do not run download commands or certify clean installation of every pinned simulator dependency graph. The existing native parity report records bounded real simulator probes for 18 non-Isaac environments. BEHAVIOR-1K and RoboDojo still lack RTX/asset-backed validation. No new full-suite accuracy claims are made. No evaluation process was restarted and no files were synchronized into the running server checkout.
