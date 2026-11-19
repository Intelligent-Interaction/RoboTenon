"""MolmoSpaces resource versions survive isolated Graph enumeration."""
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "extensions" / "benchmarks" / "molmospaces" / "src"))

from robotenon_molmospaces.benchmark import MolmoSpacesBenchmark


@pytest.fixture
def fake_constants(monkeypatch):
    constants = types.ModuleType("molmo_spaces.molmo_spaces_constants")
    constants.DATA_TYPE_TO_SOURCE_TO_VERSION = {"old": {"source": "version"}}
    package = types.ModuleType("molmo_spaces")
    package.molmo_spaces_constants = constants
    monkeypatch.setitem(sys.modules, "molmo_spaces", package)
    monkeypatch.setitem(sys.modules, "molmo_spaces.molmo_spaces_constants", constants)
    return constants


def test_resource_manifest_mapping_replaces_packaged_defaults(fake_constants):
    manifest = {"benchmarks": {"molmospaces-bench-v2": "20260415"}}
    MolmoSpacesBenchmark._configure_resource_versions(manifest)
    assert fake_constants.DATA_TYPE_TO_SOURCE_TO_VERSION == manifest


def test_resource_manifest_file_replaces_packaged_defaults(tmp_path, fake_constants):
    manifest = {"objects": {"objaverse": "20260131"}}
    path = tmp_path / "resources.json"
    path.write_text(json.dumps(manifest))
    MolmoSpacesBenchmark._configure_resource_versions(str(path))
    assert fake_constants.DATA_TYPE_TO_SOURCE_TO_VERSION == manifest


@pytest.mark.parametrize("manifest", [{}, {"objects": []}, {"objects": {"objaverse": 1}}, 3])
def test_invalid_resource_manifest_is_rejected(manifest, fake_constants):
    error = TypeError if manifest == 3 else ValueError
    with pytest.raises(error):
        MolmoSpacesBenchmark._configure_resource_versions(manifest)
