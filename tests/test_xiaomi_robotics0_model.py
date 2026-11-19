import importlib.util
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "extensions/benchmarks/models/xiaomi-robotics0/implementation.py"


def load_impl():
    spec = importlib.util.spec_from_file_location("xiaomi_robotics0_test", IMPL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(image_value=0, description="pick up the bowl"):
    image = np.full((256, 256, 3), image_value, dtype=np.uint8)
    return {
        "observation": {
            "history": [
                {
                    "images": {"agentview": image, "wrist": image.copy()},
                    "states": np.arange(8, dtype=np.float64),
                }
            ],
            "task_description": description,
        }
    }


def test_official_fields_state_and_seed_are_stable():
    module = load_impl()
    fields, seed = module.build_model_inputs(request())
    assert fields["task_id"] == "libero_all"
    assert fields["language"] == "Pick up the bowl"
    assert fields["state"].shape == (32,)
    assert fields["state"].dtype == np.float64
    assert np.array_equal(fields["state"][:8], np.arange(8))
    assert np.count_nonzero(fields["state"][8:]) == 0
    assert isinstance(fields["base"], Image.Image)
    assert seed == module.hash_data_to_seed(fields)
    assert seed == module.build_model_inputs(request())[1]
    assert seed != module.build_model_inputs(request(image_value=1))[1]


def test_prompt_matches_two_view_no_cot_protocol():
    module = load_impl()
    prompt = module.PROMPT.format(language="Pick up the bowl.")
    assert prompt.count("<|image_pad|>") == 2
    assert "Pick up the bowl. /no_cot" in prompt
    assert prompt.endswith("<cot></cot><|im_end|>\n")


def test_input_validation_rejects_bad_history_image_and_state():
    module = load_impl()
    bad = request()
    bad["observation"]["history"] = []
    with pytest.raises(ValueError, match="exactly one"):
        module.build_model_inputs(bad)

    bad = request()
    bad["observation"]["history"][0]["images"]["wrist"] = np.zeros((10, 10, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="wrist image"):
        module.build_model_inputs(bad)

    bad = request()
    bad["observation"]["history"][0]["states"] = np.zeros(7)
    with pytest.raises(ValueError, match=r"shape \(8,\)"):
        module.build_model_inputs(bad)
