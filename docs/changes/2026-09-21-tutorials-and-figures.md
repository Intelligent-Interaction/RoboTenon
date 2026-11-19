# Tutorials and figures (2026-09-21)

[**English**](2026-09-21-tutorials-and-figures.md) · [简体中文](2026-09-21-tutorials-and-figures.zh-CN.md)

This update adds CLI, Web Portal, results, and hardware deployment tutorials, plus technical diagrams and screenshots for the user documentation.

## Changes

- [Running experiments from the CLI](../tutorials/cli-experiments.md) covers preflight checks, batch and single runs, reading results, and the read-only query API.
- The revised [Portal tutorial](../tutorials/03-portal.md) starts with a CPU experiment that requires no GPU and then switches to RoboCasa.
- [Viewing experiment results](../tutorials/view-results.md) explains status, success signals, attempts, traces, metrics, images, and videos using the corresponding files and Portal pages.
- [Physical robot deployment](../tutorials/real-robot.md) covers site drivers, observation sources, plugin assembly, the action contract, stop acceptance tests, and two startup methods.
- The CLI and Portal tutorials share a [CPU experiment configuration](../tutorials/examples/cpu-demo.json).
- The [image directory](../assets/README.md) contains 15 SVG diagrams and 4 screenshots from the running Portal.
- The landing page, tutorial index, and relevant reference pages received updated links and figures. Historical audit evidence remains in this directory.

## Verification

Verification used an isolated temporary directory on macOS with Python 3.11, without a GPU, model, or robot connection.

| Check | Result |
| --- | --- |
| CLI tutorial execution and read commands | 10 command blocks passed. The query interface used a temporary port. |
| CPU batch experiment | 3 tasks completed, 3 successes, 0 errors |
| Single CPU experiment | Graph completed, task succeeded, and the actual result and data source were readable |
| Portal creation, preflight, and submission | 3 / 3 tasks succeeded |
| Portal task and node views | All three nodes completed. Inputs and outputs were visible. |
| SQL in the results tutorial | All 3 queries returned the expected records |
| Read-only HTTP queries | GET Trace and POST SQL returned records |
| Figures | 15 SVGs checked for rendering and 4 screenshots compared against live pages |

Checks passed for local Markdown links and anchors, JSON and shell blocks in current documentation, and whitespace in the documentation diff. The temporary browser tabs and demo service were closed after verification.

## Corrections based on observed behavior

A single run on the process backend uses `--output` as the output base. The actual result is in the attempt directory, which can be located using the returned `output_path`.

The read-only service exposes Trace, SQL, and Metrics APIs. Its landing page still shows the full experiment list. Disabling experiment management prevents full Portal operation. CLI results do not register automatically in the Portal experiment list.

The CPU example has no sensor images. Invalid media items in the trajectory do not represent camera output. Success counts in the screenshots establish only that the environment readiness check passed.

The hardware chapter covers integration and acceptance for a site-specific driver. The template does not establish support for arbitrary devices. This round did not run GPU inference, simulation rendering, or physical robot acceptance tests.
