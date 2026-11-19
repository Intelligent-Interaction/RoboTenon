"""Dependency-light Xiaomi RoboCasa365 and VLABench input protocols."""

from __future__ import annotations

from collections import deque
from typing import Any

import numpy as np
from PIL import Image

CAMERA_KEYS = (
    "video.robot0_agentview_left",
    "video.robot0_agentview_right",
    "video.robot0_eye_in_hand",
)

VLABENCH_CAMERA_KEYS = ("ego", "base", "left_wrist")
VLABENCH_POSITION_OFFSET = np.asarray([0.0, -0.4, 0.78], dtype=np.float32)


def quat_xyzw_to_axis_angle(quaternion: np.ndarray) -> np.ndarray:
    quaternion = np.asarray(quaternion, dtype=np.float64).reshape(-1)
    norm = np.linalg.norm(quaternion)
    if norm < 1e-12:
        return np.zeros(3, dtype=np.float32)
    quaternion = quaternion / norm
    if quaternion[3] < 0:
        quaternion = -quaternion
    xyz = quaternion[:3]
    sin_half = np.linalg.norm(xyz)
    if sin_half < 1e-12:
        return np.zeros(3, dtype=np.float32)
    angle = 2.0 * np.arctan2(
        sin_half, np.clip(quaternion[3], -1.0, 1.0)
    )
    return (xyz / sin_half * angle).astype(np.float32)


def observation_to_state(observation: dict[str, Any]) -> np.ndarray:
    state = np.concatenate(
        [
            np.asarray(
                observation["state.end_effector_position_relative"],
                dtype=np.float32,
            ).reshape(-1),
            quat_xyzw_to_axis_angle(
                observation["state.end_effector_rotation_relative"]
            ),
            np.asarray(observation["state.gripper_qpos"], dtype=np.float32).reshape(
                -1
            ),
            np.asarray(observation["state.base_position"], dtype=np.float32).reshape(
                -1
            ),
            quat_xyzw_to_axis_angle(observation["state.base_rotation"]),
        ]
    ).astype(np.float32)
    if state.shape != (14,):
        raise ValueError(f"Expected a 14D RoboCasa365 state, got {state.shape}")
    return state


def sample_history(history: deque[np.ndarray], length: int, interval: int) -> np.ndarray:
    items = list(history)
    if not items:
        raise ValueError("Observation history cannot be empty")
    indices = [
        max(0, len(items) - 1 - (length - 1 - index) * interval)
        for index in range(length)
    ]
    return np.ascontiguousarray(np.stack([items[index] for index in indices], axis=0))


def center_crop(image: np.ndarray, crop_ratio: float) -> Image.Image:
    pil_image = Image.fromarray(np.asarray(image, dtype=np.uint8))
    if crop_ratio >= 1.0:
        return pil_image
    width, height = pil_image.size
    crop_width = max(1, int(width * crop_ratio))
    crop_height = max(1, int(height * crop_ratio))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    cropped = pil_image.crop((left, top, left + crop_width, top + crop_height))
    resampling = getattr(Image, "Resampling", Image).BILINEAR
    return cropped.resize((width, height), resampling)


def build_messages(
    image_history: dict[str, np.ndarray], instruction: str, crop_ratio: float
) -> list[dict[str, Any]]:
    videos = {
        key: [center_crop(frame, crop_ratio) for frame in image_history[key]]
        for key in CAMERA_KEYS
    }
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Left camera: "},
                {"type": "video", "video": videos[CAMERA_KEYS[0]]},
                {"type": "text", "text": "\nRight camera: "},
                {"type": "video", "video": videos[CAMERA_KEYS[1]]},
                {"type": "text", "text": "\nWrist camera: "},
                {"type": "video", "video": videos[CAMERA_KEYS[2]]},
                {
                    "type": "text",
                    "text": (
                        "\n\nGenerate robot actions for the task:\n"
                        f"{instruction} /no_cot"
                    ),
                },
            ],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "<cot></cot>"}],
        },
    ]


def quaternion_wxyz_to_euler(quaternion: np.ndarray) -> np.ndarray:
    """Match VLABench's scipy ``Rotation.as_euler('xyz')`` convention."""
    quaternion = np.asarray(quaternion, dtype=np.float64).reshape(-1)
    if quaternion.size != 4:
        raise ValueError(f"Expected a 4D wxyz quaternion, got {quaternion.shape}")
    norm = np.linalg.norm(quaternion)
    if norm < 1e-12:
        raise ValueError("VLABench end-effector quaternion has zero norm")
    w, x, y, z = quaternion / norm
    roll = np.arctan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    pitch = np.arcsin(np.clip(2.0 * (w * y - z * x), -1.0, 1.0))
    yaw = np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return np.asarray([roll, pitch, yaw], dtype=np.float32)


def vlabench_observation_to_state(
    observation: dict[str, Any],
    position_offset: np.ndarray = VLABENCH_POSITION_OFFSET,
) -> np.ndarray:
    ee_state = np.asarray(observation["ee_state"], dtype=np.float32).reshape(-1)
    if ee_state.size < 8:
        raise ValueError(
            "Expected VLABench ee_state=[xyz, quaternion_wxyz, gripper] "
            f"with at least 8 values, got {ee_state.size}"
        )
    state = np.concatenate(
        [
            ee_state[:3] - np.asarray(position_offset, dtype=np.float32),
            quaternion_wxyz_to_euler(ee_state[3:7]),
            ee_state[7:8],
        ]
    ).astype(np.float32)
    if state.shape != (7,):
        raise ValueError(f"Expected a 7D VLABench state, got {state.shape}")
    return state


def _to_rgb_pil(image: np.ndarray, image_size: int) -> Image.Image:
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[-1] not in (1, 3, 4):
        raise ValueError(f"Expected an HWC image, got {array.shape}")
    if np.issubdtype(array.dtype, np.floating):
        maximum = float(np.nanmax(array)) if array.size else 0.0
        array = np.clip(array, 0.0, 1.0 if maximum <= 1.0 + 1e-6 else 255.0)
        if maximum <= 1.0 + 1e-6:
            array = array * 255.0
    array = array.astype(np.uint8, copy=False)
    image = Image.fromarray(array[..., 0] if array.shape[-1] == 1 else array)
    image = image.convert("RGB")
    if image_size > 0 and image.size != (image_size, image_size):
        resampling = getattr(Image, "Resampling", Image).BILINEAR
        image = image.resize((image_size, image_size), resampling)
    return image


def build_vlabench_messages(
    images: dict[str, np.ndarray],
    instruction: str,
    *,
    image_size: int = 480,
    cot: bool = False,
) -> list[dict[str, Any]]:
    converted = [_to_rgb_pil(images[key], image_size) for key in VLABENCH_CAMERA_KEYS]
    suffix = "/cot" if cot else "/no_cot"
    content = [
        {"type": "text", "text": "The following observations are captured from multiple views.\n# Ego View\n"},
        {"type": "image", "image": converted[0]},
        {"type": "text", "text": "\n# Base View\n"},
        {"type": "image", "image": converted[1]},
        {"type": "text", "text": "\n# Left-Wrist View\n"},
        {"type": "image", "image": converted[2]},
        {"type": "text", "text": f"\nGenerate robot actions for the task:\n{instruction} {suffix}"},
    ]
    messages = [{"role": "user", "content": content}]
    if not cot:
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "<cot></cot>"}]}
        )
    return messages


__all__ = [
    "CAMERA_KEYS",
    "VLABENCH_CAMERA_KEYS",
    "VLABENCH_POSITION_OFFSET",
    "build_messages",
    "build_vlabench_messages",
    "center_crop",
    "observation_to_state",
    "quaternion_wxyz_to_euler",
    "quat_xyzw_to_axis_angle",
    "sample_history",
    "vlabench_observation_to_state",
]
