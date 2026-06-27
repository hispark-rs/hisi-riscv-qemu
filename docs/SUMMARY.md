# Summary

[文档首页](README.md)

# 教程 · 学习导向

- [教程 1：环境准备与安装](tutorials/01-install.md)
- [教程 2：跑通第一个固件](tutorials/02-first-run.md)

# 操作指南 · 任务导向

- [安装与构建仿真器](how-to/install-and-build.md)
- [运行固件](how-to/run-firmware.md)
- [调试与追踪](how-to/debug-and-trace.md)
- [运行测试](how-to/run-tests.md)
- [扩展掩膜 ROM 桩](how-to/extend-rom-stubs.md)
- [移植到新的 QEMU 版本](how-to/port-qemu-version.md)

# 参考 · 信息导向

- [内存映射与外设基址](reference/memory-map.md)
- [机器模型](reference/machine-model.md)
- [外设建模矩阵](reference/peripheral-matrix.md)
- [xlinx 自定义 ISA](reference/xlinx-isa.md)
- [掩膜 ROM 桩目录](reference/rom-stubs.md)
- [运行选项](reference/run-options.md)
- [验证覆盖范围](reference/verification.md)

# 解释 · 理解导向

- [设计取舍：为什么 fork QEMU](explanation/design-rationale.md)
- [中断控制器设计](explanation/interrupt-controller.md)
- [已知边界与非目标](explanation/limitations.md)
- [多角度对齐分析](explanation/alignment-analysis.md)
- [Rust 工具链是否需要 xlinx](explanation/rust-toolchain-xlinx.md)
- [BS21 连接性仿真可行性](explanation/bs21-connectivity-feasibility.md)
- [BS21 厂商固件运行现状](explanation/bs21-vendor-firmware.md)
