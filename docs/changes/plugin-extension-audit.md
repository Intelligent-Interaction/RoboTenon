# Plugin integration boundaries and repairs (2026-09-20)

[**English**](plugin-extension-audit.md) · [简体中文](plugin-extension-audit.zh-CN.md)

The integration goal is to add environments, datasets, models, drivers, execution backends, storage, and stream transports using existing public protocols.
Add plugins and configuration without provider-name branches in Runner or the experiment entry point. See [infrastructure plugins](../infrastructure-plugins.md) for interfaces.

| Area | Implementation |
| --- | --- |
| Six application provider types | External packages load through plugin_* imports without modifying load_builtin_* |
| Environment parameters | Configuration → TaskCase → EpisodeRequest merges recursively into SessionSpec. Canonical fields take precedence. |
| Batch and CLI | Override TaskAdapter by instance ID/registered name. Custom splits are supported. Providers declare shortcuts in cli_overrides. |
| Execution backend | Registered factory and capability metadata. Single and batch share execute/close. Launchers do not import the process implementation directly. |
| Four storage types | Register safety/context/actions/observation independently. Select plugin_stores/backend/params in configuration. |
| RPC stream transport | JSON-lines service protocol is independent of connection and listener implementations. Both Body execution and control connections use the same transport configuration. |
| Observation modalities | Frame.modality/producer and Channel.modality accept nonempty strings and retain explicitly declared modalities instead of inferring other. |
| Resource lifecycle | Common application resource container handles startup failure and normal closure. Custom stores can close synchronously or asynchronously. |
| Actions and models | The 12-dimensional constraint stays with RoboCasa Body/Xiaomi Tool. Each new plugin declares its IO and action format. |
| Results | Graph outputs map explicitly. Environment and Body convert native success/timeout. |

Verification covered sequential and spawned multiprocess cases with external Environment/TaskAdapter, single/batch external execution backends,
assembly/paths/close for all four storage types, startup cleanup, action-store restart, external stream transport for Environment/Body RPC,
custom sensor round trips from Frame through cache/resolver, and architecture tests forbidding entry-point dispatch by backend name.
See the [verification record](verification-2026-09.md) for the corresponding full regression counts.

Common contracts and deployment limits:

- Graph scheduling semantics, safety levels, JSON serialization, one in-process environment, and Body action boundaries are shared compatibility contracts.
- Capabilities such as shared are provider commitments. Providers must supply any distributed consistency, authentication, scheduling, hardware protection, or driver their implementation needs.
- Built-in Web/Trace queries use explicit SQLite/JSONL sources. A new store needs a compatible projection or query adapter for display in the existing Portal.
- Built-in observation memory/filesystem names are legacy aliases for hybrid. Custom caches can be implemented separately. remote requires an actual remote endpoint.
- Isolate incompatible Python/CUDA/MuJoCo dependencies in separate deployments. This round did not implement LIBERO or run real GPU/robot experiments.
