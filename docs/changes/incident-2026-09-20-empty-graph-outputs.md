# Legacy Graph override produced zero recorded successes

[**English**](incident-2026-09-20-empty-graph-outputs.md) · [简体中文](incident-2026-09-20-empty-graph-outputs.zh-CN.md)

Affected experiment: `exp-20260920-140038-9536464d`.

The override included a complete legacy Graph array without exposed outputs. The updated runner read success from GraphResult.outputs. An empty dict produced task_success=false, env_steps=0, and reason=null. Environment feedback and raw terminal-node results survived. Of the first 23 completed tasks examined, 19 recorded environment_success and 4 reached horizon.

## Prevention

Portal submission, experiment entry, pre-Worker model initialization, and episode entry now check that the selected Graph declares outputs.success. A missing declaration raises a configuration error. This result contract applies to the experiment runner only. Forking old parameters requires an explicit Graph mapping update. Node names are insufficient to infer the mapping.

## Recovering the original results

The experiment root's result_recovery.json stores an explicit graphs mapping. If present, the Portal resolves mapped outputs from node_records in context.root_scope for completed records with empty outputs. It skips errors, cancellations, running tasks, and existing public outputs. Recovered signals must be booleans. Summary reading checks episode_json and generation_started_at_ns to keep evidence from different rerun generations separate.

Back up original JSON before repair. A live legacy Coordinator retains old counts in memory, so replacing summary.json once is insufficient. The Portal applies the mapping on each summary read and recomputes successes and rates. This also covers later results from legacy Workers without interrupting simulation. Use the recovered API results when raw summary.json still reflects the old Coordinator. Recovery preserves trajectories and execution configuration and does not count completion alone as success.
