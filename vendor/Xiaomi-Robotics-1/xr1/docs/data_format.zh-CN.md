# 训练数据格式

[English](data_format.md) · [**简体中文**](data_format.zh-CN.md)

每个 episode 包含一份 JSON 标注和三条同步视频。支持绝对视频路径。相对路径按启动训练时的工作目录解析。

## 示例

```json
{
  "trajectory_type": "success",
  "time": "2026-01-01_09_00_01",
  "num_frames": 1000,
  "instruction": {
    "general": [
      {
        "images": [
          "observations.ego",
          "observations.wrist_left",
          "observations.wrist_right"
        ],
        "conversations": [
          {
            "from": "human",
            "value": "The following observations are captured from multiple views.\n# Ego View\n<image>\n# Left-Wrist View\n<image>\n# Right-Wrist View\n<image>\nGenerate robot actions for the task:\nload the washer"
          },
          {"from": "gpt", "value": ""}
        ]
      }
    ]
  },
  "observations": {
    "ego": [
      {
        "path": "data/videos/episode_001_ego.mp4",
        "start": 0,
        "crop_bbox": null
      }
    ],
    "wrist_left": [
      {
        "path": "data/videos/episode_001_left.mp4",
        "start": 0,
        "crop_bbox": null
      }
    ],
    "wrist_right": [
      {
        "path": "data/videos/episode_001_right.mp4",
        "start": 0,
        "crop_bbox": null
      }
    ]
  },
  "proprios": {
    "left_ee_pos": [],
    "left_ee_rotm": [],
    "left_arm_joint": [],
    "left_gripper_pos": [],
    "right_ee_pos": [],
    "right_ee_rotm": [],
    "right_arm_joint": [],
    "right_gripper_pos": [],
    "waist_pos": []
  },
  "actions": {
    "left_ee_pos": [],
    "left_ee_rotm": [],
    "left_gripper_pos": [],
    "right_ee_pos": [],
    "right_ee_rotm": [],
    "right_gripper_pos": [],
    "waist_pos": [],
    "base_vel": []
  }
}
```

必须提供 `num_frames`、`instruction.general`、`images` 引用的三条视频记录，以及 `proprios` 和 `actions` 下示例中的全部数组。这些数组均为每帧一个条目。旋转矩阵按 9 个值展平，单臂关节最多包含 7 个值。

每条视频记录都必须包含 `path`。`start` 默认为 `0`，`crop_bbox` 默认不裁剪。

`instruction.general` 中的每个 `<image>` 占位符按顺序使用 `images` 列表的下一项。

空的 assistant 消息用于补全提示词模板，具体内容由数据加载器填入。

`crop_bbox` 可为 `null`，也可设为 `(top, left, height, width)`。超出画面的部分用零填充。

## 动作布局

动作转换为相对增量后打包为 `(30, 60)` 张量。

| 维度 | 内容 |
|---|---|
| 0–2 | 左侧末端执行器位置 |
| 3–5 | 左侧末端执行器轴角旋转 |
| 6 | 左夹爪 |
| 8–10 | 右侧末端执行器位置 |
| 11–13 | 右侧末端执行器轴角旋转 |
| 14 | 右夹爪 |
| 16 | 腰部 |
| 17–19 | 底盘速度 |
| 其余 | 保留 |

数据配置使用逐步的 `mean` 和 `std` 归一化打包后的相对动作，使用 `q01` 和 `q99` 处理打包后的机器人状态。
