#!/usr/bin/env python3
"""Generate the official 50-task Easy and Hard RoboTwin 2.0 profiles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


TASKS = (
    "adjust_bottle",
    "beat_block_hammer",
    "blocks_ranking_rgb",
    "blocks_ranking_size",
    "click_alarmclock",
    "click_bell",
    "dump_bin_bigbin",
    "grab_roller",
    "handover_block",
    "handover_mic",
    "hanging_mug",
    "lift_pot",
    "move_can_pot",
    "move_pillbottle_pad",
    "move_playingcard_away",
    "move_stapler_pad",
    "open_laptop",
    "open_microwave",
    "pick_diverse_bottles",
    "pick_dual_bottles",
    "place_a2b_left",
    "place_a2b_right",
    "place_bread_basket",
    "place_bread_skillet",
    "place_burger_fries",
    "place_can_basket",
    "place_cans_plasticbox",
    "place_container_plate",
    "place_dual_shoes",
    "place_empty_cup",
    "place_fan",
    "place_mouse_pad",
    "place_object_basket",
    "place_object_scale",
    "place_object_stand",
    "place_phone_stand",
    "place_shoe",
    "press_stapler",
    "put_bottles_dustbin",
    "put_object_cabinet",
    "rotate_qrcode",
    "scan_object",
    "shake_bottle",
    "shake_bottle_horizontally",
    "stack_blocks_three",
    "stack_blocks_two",
    "stack_bowls_three",
    "stack_bowls_two",
    "stamp_seal",
    "turn_switch",
)


def profile(task_name: str, task_config: str) -> dict:
    return {
        "episodes_per_task": 1,
        "params": {
            "task_name": task_name,
            "task_config": task_config,
            "seed": 0,
            "instruction_type": "seen",
            "test_num": 100,
            "skip_expert_check": True,
            "fast_init": True,
            "fast_render": False,
        },
        "benchmark_id": "robotwin_benchmark",
        "source_config": "RoboTwin-2.0/env_cfg/eval/all_tasks.yml",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    for task_config in ("demo_clean", "demo_randomized"):
        destination = args.output / task_config
        destination.mkdir(parents=True, exist_ok=True)
        for index, task_name in enumerate(TASKS):
            path = destination / f"{index:02d}-{task_name}.json"
            path.write_text(
                json.dumps(profile(task_name, task_config), indent=2) + "\n",
                encoding="utf-8",
            )
    print(f"generated {len(TASKS) * 2} profiles under {args.output}")


if __name__ == "__main__":
    main()
