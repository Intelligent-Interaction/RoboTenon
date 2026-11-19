# Change history

[**English**](README.md) · [简体中文](README.zh-CN.md)

> This directory archives historical fixes and the evidence collected for each verification run. Personal paths, deployment directories, and private environment names in documents and attachments have been replaced by placeholders. Historical summaries and hashes describe the original snapshots and cannot verify the redacted attachments. For installation and operation, see the [documentation index](../README.md).


The archive includes migration notes, repairs, performance measurements, investigations, and audits. For everyday tasks, see the [tutorials](../tutorials/README.md) and [reference manuals](../README.md).

Implementation details, line numbers, test counts, and server information refer to the revision examined in each record. Consult the reference manuals for current interfaces.

## Migrations and repairs

| Date | Record | Scope |
| --- | --- | --- |
| 2026-09-28 | [Native simulation checks outside Isaac](native-smoke-2026-09-28.md) | Local fake actions, per-frame images, success signals, and blockers across 18 benchmarks |
| 2026-09-21 | [Framework audit repairs](framework-audit-repairs-2026-09-21.md) | Configuration, batch Graph selection, preflight, historical queries, and performance boundaries in A01–A08 |
| 2026-09-21 | [Demo data downloads](2026-09-21-demo-data-downloads.md) | Downloading scene assets, models, and optional demonstration trajectories |
| 2026-09-21 | [Tutorials and figures](2026-09-21-tutorials-and-figures.md) | CLI, Portal, result inspection, hardware deployment, and illustrations |
| 2026-09-21 | [History and batch execution repairs](history-repairs-2026-09-21.md) | Resumable cleanup scans, result recovery, SQLite experiment catalog, bounded writes, and HTTP concurrency limits |
| 2026-09-20 | [Documentation reorganization](2026-09-20-documentation.md) | Tutorials, navigation, historical archive, and example checks |
| 2026-09-20 | [Independent audit repairs](audit-repairs-2026-09-20.md) | Scope and regression evidence for R01–R11 |
| 2026-09-20 | [Performance and usability improvements](performance-2026-09-20.md) | Background stop, incremental results, pagination, cache, preflight, and Worker notifications |
| 2026-09-20 | [Plugin integration review](plugin-extension-audit.md) | Integration boundaries for application and infrastructure plugins |
| 2026-09 | [Migration notes](migration-2026-09.md) | Configuration, Graph, execution layers, and interface compatibility |
| 2026-09 | [Verification record](verification-2026-09.md) | Test environments, coverage, and limitations by phase |

## Incident investigations

| Date | Record | Scope |
| --- | --- | --- |
| 2026-09-20 | [Graph outputs and result counts](incident-2026-09-20-empty-graph-outputs.md) | Verifying and recovering results from older Graphs without exposed outputs |
| 2026-09-20 | [Queue and timing](incident-2026-09-20-queue-and-timing.md) | Observed evidence, changes, and deployment record |
| 2026-09-20 | [Native renderer crash investigation](render-crash-investigation-20260920.md) | Rendering stack, processes, and failure scope |
| 2026-09-20 | [Renderer crash recovery](render-crash-recovery-20260920.md) | OSMesa fallback and verification scope |
| 2026-09-19 | [Worker abort](incident-2026-09-19-worker-abort.md) | Worker SIGABRT, cache race, and truncated responses |

## Audits and integration assessments

| Date | Record | Evidence |
| --- | --- | --- |
| 2026-09-21 | [Framework usability, syntax, and performance audit](framework-audit-2026-09-21.md) | [Reproduction results and source fingerprints](framework-audit-2026-09-21/results.json) |
| 2026-09-20 | [Independent audit](audit-2026-09-20-independent/独立审计.md) | [Reproduction and verification details](audit-2026-09-20-independent/verification.json) |
| 2026-09-20 | [Embodied systems review and project audit](audit-2026-09-20/具身系统调研与项目审计.md) | [Reproduction script](audit-2026-09-20/reproduce.py) |
| 2026-09-20 | [LIBERO integration assessment](libero-integration-review.md) | Proposed scope, dependencies, and implementation effort at the time of review |
| 2026-09-18 | [Independent audit](audit-2026-09-18/独立审计.md) | [Reproduction script](audit-2026-09-18/reproduce.py) |
| 2026-09-15 | [Architecture audit](audit-2026-09-15/独立架构审计.md) | [Reproduction script](audit-2026-09-15/reproduce.py) |

Logs, hash manifests, patches, and data files accompany the relevant records. Archiving retained the original attachment content. Paths and commands in the hash manifests describe the workspace at the time and were not rewritten during migration.

Historical `reproduce.py` scripts contain revision-specific assertions, some of which pass when they reproduce a defect. Use them to investigate the original behavior. The current regression suite lives in `tests/`. For older scripts, set `PYTHONPATH=.` from the repository root so their former directory layout does not interfere with imports.

## Adding a record

Include the date in the Markdown filename and document the change, compatibility impact, verification environment, and untested scope. Put attachments in a directory with the same base name. Update the relevant reference page to describe current behavior and keep detailed test counts and repair procedures in the change record.
