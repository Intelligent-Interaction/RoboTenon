from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..infra.params import ParameterSnapshot
from ..schemas import ExperimentSpec


@dataclass(frozen=True, slots=True)
class FrozenExperiment:
    spec: ExperimentSpec
    parameters: ParameterSnapshot
    content_hash: str


def freeze_experiment(spec: ExperimentSpec, *, defaults: dict | None = None, overrides: dict | None = None) -> FrozenExperiment:
    values = dict(defaults or {})
    values.update(spec.parameters)
    values.update(overrides or {})
    parameters = ParameterSnapshot(name=spec.experiment_id, values=values)
    payload = {"spec": spec.model_dump(mode="json"), "parameters": parameters.model_dump(mode="json"), "parameter_hash": parameters.content_hash}
    content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return FrozenExperiment(spec=spec, parameters=parameters, content_hash=content_hash)
