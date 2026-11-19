# Demo data downloads (2026-09-21)

[**English**](2026-09-21-demo-data-downloads.md) · [简体中文](2026-09-21-demo-data-downloads.zh-CN.md)

The CLI guide, Portal guide, simulation tutorial, and documentation index now link to instructions for downloading RoboCasa scene assets, model weights, and optional demonstration trajectories. The CPU demo requires no external data.

The guide recommends keeping large models outside the repository, using a separate Hugging Face CLI environment, previewing the size before downloading, and verifying the weights. The upstream RoboCasa scripts install assets into the RoboCasa package directory. The guide explains the purpose and default location of demonstration trajectories and how to preview them using the upstream data script.

The commands and downloader options follow the official RoboCasa installation and data instructions, upstream scripts, and Hugging Face Hub CLI documentation. This documentation-only change did not download large assets or models or run GPU, simulation, or hardware checks.
