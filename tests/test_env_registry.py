from uuid import uuid4

from robotenon.envs import EnvironmentRegistry, register_environment
from robotenon.schemas import EnvironmentDescriptor


class _Adapter:
    def __init__(self, endpoint: str = "local"):
        self.endpoint = endpoint
        self.descriptor = EnvironmentDescriptor(env_id="registry-test", display_name=endpoint)


def test_environment_registry_caches_adapter_factory_once():
    registry = EnvironmentRegistry()
    constructed = []

    def factory(endpoint="local"):
        constructed.append(endpoint)
        return _Adapter(endpoint)

    registry.register("registry-test", factory)
    first = registry.get("registry-test", endpoint="sim")
    second = registry.get("registry-test")

    assert first is second
    assert constructed == ["sim"]
    assert registry.names() == ["registry-test"]


def test_environment_registration_is_separate_from_module_registry():
    # A decorator registration returns the factory and does not construct an
    # adapter until the first lookup.
    local_id = f"registry-decorator-{uuid4()}"

    @register_environment(local_id)
    class DecoratedAdapter(_Adapter):
        def __init__(self):
            super().__init__("decorated")
            self.descriptor = EnvironmentDescriptor(env_id=local_id)

    from robotenon.envs import environment_registry

    adapter = environment_registry.get(local_id)
    assert adapter.descriptor.env_id == local_id
