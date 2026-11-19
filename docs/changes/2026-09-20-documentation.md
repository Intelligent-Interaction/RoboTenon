# Documentation reorganization (2026-09-20)

[**English**](2026-09-20-documentation.md) · [简体中文](2026-09-20-documentation.zh-CN.md)

## Scope

- The landing page retains the project overview, minimal command to run the framework, and documentation links.
- A new tutorial index covers installation and Fake, the first RoboCasa task, the Portal, custom Modules, and deployment on 8 GPUs.
- The quick start covers installation, execution, configuration checks, and next steps. The tutorials contain the detailed procedures.
- Migration notes, audits, investigations, repairs, and performance records moved to `docs/changes/` with their supporting evidence.
- The developer guide gained section navigation. Dated preambles were removed from reference pages, and current interface descriptions moved into the relevant sections.
- The FAQ now explains Fake output, experiment records, read-only data sources, and the directory managed by the Portal.

## File locations and compatibility

Reference manuals for architecture, execution, Modules, and plugins retain their original paths. Historical documents moved to `docs/changes/`. Markdown links in the repository were updated. External bookmarks pointing to the old locations need updating.

The edits covered documentation only. Code, configuration, experiment data, and third-party documentation were outside their scope. Historical evidence was preserved, including the test counts and paths specific to each version.

## Example verification

The examples were verified on macOS with Python 3.11. CLI examples used copies of the configurations in a temporary directory to isolate new run data from existing experiments.

| Check | Result |
| --- | --- |
| 287 local links and heading anchors across 40 Markdown files | Passed |
| 51 JSON blocks from the user documentation | Syntax checks passed |
| 63 Bash blocks from the user documentation | `bash -n` passed |
| Fake, experiment overlays, four component types, custom Graph and registration, and two CLI help entry points | 10 checks passed |
| Component and Graph tests referenced by tutorials | 19 passed |
| Single-task Portal override | Loaded successfully with 1 task and 1 trial |
| 46 historical evidence attachments | Byte-for-byte match with pre-archive contents |
| Whitespace check on documentation changes | Passed |

For RoboCasa, real models, GPUs, and physical robots, the review checked command arguments, configuration, and environment variables against the code without starting those services. This pass did not run the full framework regression suite. Earlier full-suite results are available in the corresponding records.
