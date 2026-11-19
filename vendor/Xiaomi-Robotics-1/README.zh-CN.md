[English](README.md) · [**简体中文**](README.zh-CN.md)

<div align="center">

  # Xiaomi-Robotics-1

  **突破数据瓶颈：通过与机器人本体无关的预训练扩展策略模型**

  [![Paper](https://img.shields.io/badge/📄-Paper-red)](https://arxiv.org/abs/2607.15330)
  [![Project Page](https://img.shields.io/badge/🌐-Project_Page-blue)](https://robotics.xiaomi.com/xiaomi-robotics-1.html)
  [![Hugging Face](https://img.shields.io/badge/%F0%9F%A4%97-Hugging%20Face-yellow)](https://huggingface.co/collections/XiaomiRobotics/xiaomi-robotics-1)
  [![ModelScope Collection](https://img.shields.io/badge/ModelScope-Collection-624aff.svg?logo=modelscope&logoColor=white)](https://www.modelscope.cn/collections/XiaomiRobotics/Xiaomi-Robotics-1)
  [![License](https://img.shields.io/badge/License-Apache_2.0-green.svg)](LICENSE)
  [![Papers with Code: SOTA on RoboCasa](https://paperswithcode.co/api/v1/papers/2607.15330/leaderboard-badge.svg?eval=18089&live=1)](https://paperswithcode.co/api/v1/papers/2607.15330/leaderboard-badge-link?eval=18089)

</div>

---

## 💡 Xiaomi-Robotics-1 简介

**Xiaomi-Robotics-1** 是一个机器人基础模型，训练数据包含**超过 10 万小时的真实操作轨迹**。这是一个**视觉–语言–动作（VLA）**模型，旨在直接应对未见环境中的移动操作，并用少量数据适配新任务。

XR-1 借鉴大语言模型的两阶段训练方法，先通过**预训练**扩大能力覆盖范围，再通过**后训练**对齐实际机器人与任务。作者报告，扩大预训练规模所带来的收益在后训练后仍能体现于真机表现，且在已测规模内**尚未出现饱和迹象**。

XR-1 使用**混合 Transformer（MoT）**，将预训练视觉语言模型 **Qwen3-VL** 与扩散 Transformer（DiT）连接起来。DiT 与视觉语言模型层数相同，但隐藏层维度较小，以加快推理。

### 主要特点

* **🧠 泛化能力。** 预训练使用超过 10 万小时与机器人本体无关的 UMI 轨迹，覆盖家庭、商业、工业和户外等 1,700 多个场景，支持在未见场景中执行复杂任务。
* **🚀 实时执行。** 通过异步执行降低推理延迟。
* **🛠️ 部署方式。** 模型基于 Hugging Face `transformers`（固定版本 `4.57.1`），针对消费级 GPU 进行了优化。
* **⚡ 少样本适配。** 每个任务使用数小时的演示数据即可适配新的复杂任务。

### 两阶段训练

**第一阶段：预训练。** 利用与本体无关的 UMI 数据，学习可泛化的动作生成能力。基于视觉语言模型的自动标注流程将长轨迹切分为固定长度的片段，并为每段生成状态变化描述。模型据此学习生成动作，将场景从当前状态推进到描述的目标状态。这一流程降低了大规模人工标注的成本，缓解了机器人策略模型扩大训练规模时面临的数据不足问题。

**第二阶段：后训练。** 使用内部真机数据、筛选后的开源机器人数据，以及高质量人工标注 UMI 数据，沿两个方向对齐预训练模型。
- *本体对齐*：通过跨本体数据，将通用动作生成能力映射到实际机器人上。
- *指令对齐*：使模型从依赖场景变化描述，转为理解自然语言指令并直接执行。

后训练完成后，XR-1 可以在未见环境中对未见物体执行移动操作。该模型先使用**超过 10 万小时的 UMI 轨迹**进行预训练，再使用**超过 1 万小时的跨本体数据**进行后训练。[Hugging Face 开源模型](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-5B)


## 📅 更新记录

- **[2026-08-03]** 💻 发布后训练、推理和基准评测的代码与模型权重。
- **[2026-07-16]** 📄 发布技术报告（[arXiv:2607.15330](https://arxiv.org/abs/2607.15330)）。

---


## 🏆 基准评测

上游团队在 **RoboCasa**、**RoboCasa365**、**VLABench** 和 **RoboDojo** 四个标准仿真基准上评测了 **Xiaomi-Robotics-1**，并报告 XR-1 在四者上均达到当时的最佳结果。截至 2026 年 7 月 15 日，XR-1 在 RoboCasa365 和 RoboDojo 的排行榜上均排名第一。

| 基准 | XR-1 | 次优结果 | 相对提升 |
| :--- | :---: | :---: | :---: |
| RoboCasa | 74.5% | 72.6% | +2.6% |
| RoboCasa365 | 57.4% | 46.6% | +23.2% |
| VLABench | 59.1% | 53.2% | +11.1% |
| RoboDojo | 13.93% | 8.80% | +58.3% |

各基准对应的微调权重与评测指南如下。

| 基准 | 模型权重 | 评测指南 |
| :--- | :--- | :--- |
| RoboCasa | [Xiaomi-Robotics-1-RoboCasa](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa) | [`eval_robocasa/README.md`](eval_robocasa/README.zh-CN.md) |
| RoboCasa365 | [Xiaomi-Robotics-1-RoboCasa365](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-RoboCasa365) | [`eval_robocasa365/README.md`](eval_robocasa365/README.zh-CN.md) |
| VLABench | [Xiaomi-Robotics-1-VLABench](https://huggingface.co/XiaomiRobotics/Xiaomi-Robotics-1-VLABench) | [`eval_vlabench/README.md`](eval_vlabench/README.zh-CN.md) |
| RoboDojo | — | [上游官方文档](https://github.com/robodojo-benchmark/RoboDojo) |

### 🏠 适配新的真机任务

在这些实验中，XR-1 使用少量演示数据适配新的复杂任务，整体成功率高于 π0.5 基线。

| 任务 | XR-1（平均每任务 < 10 小时） | π0.5（平均每任务 < 10 小时） | XR-1（平均每任务 < 40 小时） | π0.5（平均每任务 < 40 小时） |
| :--- | :---: | :---: | :---: | :---: |
| 包装手机 | 70% | 30% | 80% | 40% |
| 为打印机补充耗材 | 70% | 20% | 60% | 20% |
| 将衣物装入洗衣机 | 80% | 40% | 100% | 50% |
| 装箱 | 80% | 70% | 100% | 100% |
| **总体** | **75%** | **40%** | **85%** | **53%** |

表中数值均为成功率，越高越好。XR-1 即 Xiaomi-Robotics-1。


## 🛠️ 后训练

在自有数据上微调 XR-1 所需的后训练和部署代码位于 [`xr1/`](xr1/README.zh-CN.md)。环境安装、模型权重转换、数据格式、训练和部署步骤见 **[`xr1/README.md`](xr1/README.zh-CN.md)**。




## 📚 引用

使用本项目时，可引用以下技术报告。

```bibtex
@article{team2026xiaomi,
  title={Xiaomi-Robotics-1: Scaling Vision-Language-Action Models with over 100K Hours of Real-World Trajectories},
  author={Team, Xiaomi Robotics and Guo, Jun and Jin, Piaopiao and Li, Jason and Li, Peiyan and Li, Yingyan and Liu, Futeng and Peng, Wanli and Qin, Optimus and Su, Yifei and others},
  journal={arXiv preprint arXiv:2607.15330},
  year={2026}
}
```


## 📄 许可证

本项目采用 [Apache License 2.0](LICENSE)。
