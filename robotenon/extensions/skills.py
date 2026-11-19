from __future__ import annotations

import hashlib
import json

from ..schemas import SkillDefinition, SkillHead


class SkillRegistry:
    """Immutable versioned skills registered independently from Workflow."""

    def __init__(self):
        self._skills: dict[tuple[str, str], SkillDefinition] = {}

    def register(self, skill: SkillDefinition) -> SkillDefinition:
        key = (skill.skill_id, skill.version)
        if key in self._skills:
            raise ValueError(f"skill version already registered: {skill.skill_id}@{skill.version}")
        if skill.content_hash is None:
            data = skill.model_dump(mode="json", exclude={"content_hash"})
            skill.content_hash = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._skills[key] = skill
        return skill

    def heads(self) -> list[SkillHead]:
        return [SkillHead.model_validate(skill.model_dump()) for skill in self._skills.values()]

    def get(self, skill_id: str, version: str) -> SkillDefinition:
        return self._skills[(skill_id, version)]
