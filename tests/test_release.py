import asyncio

from robotenon.extensions.deployment import DeploymentHealth, DeploymentManager, DeploymentStatus
from robotenon.extensions.releases import ExperimentRelease, ExperimentReleaseRegistry, ReleaseStatus


def test_release_lifecycle_and_deploy(tmp_path):
    registry = ExperimentReleaseRegistry(tmp_path / "releases")
    registry.put(ExperimentRelease(release_id="r1", name="demo", version="1", model_artifacts=["model.bin"]))
    registry.transition("demo", "1", ReleaseStatus.VALIDATING)
    registry.transition("demo", "1", ReleaseStatus.CANDIDATE)
    released = registry.transition("demo", "1", ReleaseStatus.RELEASED)
    assert released.content_hash

    class Target:
        async def start(self, release):
            self.release = release

        async def stop(self):
            pass

        async def health(self):
            return DeploymentHealth(status=DeploymentStatus.READY)

    async def scenario():
        manager = DeploymentManager(Target())
        health = await manager.deploy(released)
        assert health.status is DeploymentStatus.READY
        await manager.drain()

    asyncio.run(scenario())
