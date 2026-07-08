# ws63-qemu 文档

本文档集按 [Diátaxis](https://diataxis.fr/) 框架组织，分为四类，对应你阅读时的四种不同需求：

| 你想…… | 去 | 说明 |
|--------|----|------|
| **第一次上手**，跟着一步步把固件跑起来 | [tutorials/](tutorials/) | 学习导向：手把手的入门课 |
| **完成一个具体任务**（构建 / 运行 / 调试 / 移植…） | [how-to/](how-to/) | 任务导向：照着做的操作配方 |
| **查某个事实**（地址 / 寄存器 / 指令 / 选项…） | [reference/](reference/) | 信息导向：精确、干燥的技术清单 |
| **理解某件事为什么这样**（设计取舍 / 边界 / 可行性） | [explanation/](explanation/) | 理解导向：背景与权衡的讨论 |

> 项目是什么：在 **QEMU** 上仿真 HiSilicon **WS63**（RISC-V RV32IMFC，Wi-Fi 6 + BLE + SLE/星闪 SoC），
> 用于无硬件运行仓库内预构建 fbb_ws63 C SDK 厂商固件，也可运行
> [`ws63-rs`](https://github.com/hispark-rs/hisi-riscv-rs) 裸机固件做开发验证。
> 项目级概览见仓库根的 [README.md](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/README.md)，规划见 [ROADMAP.md](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/ROADMAP.md)，变更见 [CHANGELOG.md](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/CHANGELOG.md)。

---

## 教程（Tutorials）— 学习导向

- [教程 1：环境准备与安装](tutorials/01-install.md) —— 克隆仓库 → 装依赖 → 构建仿真器。
- [教程 2：跑通第一个固件](tutorials/02-first-run.md) —— 运行预构建固件 → 看到输出。

## 操作指南（How-to）— 任务导向

- [安装与构建仿真器](how-to/install-and-build.md) —— 下载预编译 Release，或从源码构建。
- [运行固件](how-to/run-firmware.md) —— `run.sh` 用法、运行 C SDK app、用 `NV=1` 回填分区表/NV。
- [调试与追踪](how-to/debug-and-trace.md) —— GDB（`-s -S`）、trace 事件、`qemu.log`。
- [运行测试](how-to/run-tests.md) —— 寄存器级 qtest、C SDK fixture、Rust 冒烟。
- [扩展掩膜 ROM 桩](how-to/extend-rom-stubs.md) —— 确认并新增一个 ROM 函数仿真。
- [移植到新的 QEMU 版本](how-to/port-qemu-version.md) —— 为新 tag 补一套 patch-series。

## 参考（Reference）— 信息导向

- [内存映射与外设基址](reference/memory-map.md) —— 内存区域 + 各外设 `baseAddress`。
- [机器模型](reference/machine-model.md) —— 机器组件、自定义 UART 寄存器布局、中断控制器 CSR 与 IRQ 号。
- [外设建模矩阵](reference/peripheral-matrix.md) —— 全部 35 个外设的建模状态。
- [xlinx 自定义 ISA](reference/xlinx-isa.md) —— HiSilicon 私有指令编码与语义。
- [掩膜 ROM 桩目录](reference/rom-stubs.md) —— 已仿真的 ROM 函数与设备桩清单。
- [运行选项](reference/run-options.md) —— `run.sh` 环境变量与 QEMU 命令行。
- [验证覆盖范围](reference/verification.md) —— CI 门禁、C SDK fixture、Rust 冒烟、覆盖边界。

## 解释（Explanation）— 理解导向

- [设计取舍：为什么 fork QEMU](explanation/design-rationale.md) —— 方法、为何不用 `-M virt`、按版本维护 patch-series。
- [中断控制器设计](explanation/interrupt-controller.md) —— 两类中断线（mie 类与自定义本地类）的投递机制。
- [已知边界与非目标](explanation/limitations.md) —— ROM 数据墙、语义边界、已知简化、冻结项。
- [多角度对齐分析](explanation/alignment-analysis.md) —— 用 QEMU 交叉验证 rs HAL ↔ C SDK ↔ SVD。
- [Rust 工具链是否需要 xlinx](explanation/rust-toolchain-xlinx.md) —— 调研结论与建议。
- [BS21 连接性仿真可行性](explanation/bs21-connectivity-feasibility.md) —— BLE/SLE 边界为何是死胡同。
- [BS21 厂商固件运行现状](explanation/bs21-vendor-firmware.md) —— `-M bs21` 上的签名镜像格式与边界。

## QEMU 官方文档

本项目是上游 QEMU 的 fork，通用知识以官方文档为准：

- [QEMU 文档主页](https://www.qemu.org/docs/master/) · [构建系统](https://www.qemu.org/docs/master/devel/build-system.html)
- [RISC-V 系统仿真](https://www.qemu.org/docs/master/system/target-riscv.html) · [命令行选项（Invocation）](https://www.qemu.org/docs/master/system/invocation.html)
- [GDB usage](https://www.qemu.org/docs/master/system/gdb.html) · [Tracing](https://www.qemu.org/docs/master/devel/tracing.html) · [Record/replay（icount）](https://www.qemu.org/docs/master/system/replay.html) · [Semihosting](https://www.qemu.org/docs/master/about/emulation.html) · [QTest](https://www.qemu.org/docs/master/devel/testing/qtest.html)

## 文档自身的检查（CI）

本文档集受 CI 守护（见仓库 `.github/workflows/`）：

- `docs.yml`（每次涉及 `.md` 的 push/PR）：`scripts/check-docs.py` 校验**内部链接 / 锚点 / Diátaxis 布局 / 无孤儿页 / 每页有 H1**，`markdownlint-cli2`（配置 `.markdownlint-cli2.yaml`）做**质量检查**。
- `link-check.yml`（每周 + 手动）：`lychee` 校验**外部 URL 可达性**（与 PR 解耦，避免第三方站点抖动阻塞合并）。

本地可直接跑：`python3 scripts/check-docs.py` 与 `npx markdownlint-cli2`。

---

真值来源：内存布局 = `ws63-rs/ws63-rt/{memory.x,layout.ld}` 与 fbb_ws63 C SDK 板级配置相互对齐；
外设基址/寄存器 = `ws63-rs/ws63-pac/ws63-svd/WS63.svd`；寄存器行为 = fbb_ws63 C SDK `hal_*_regs_def.h` +
`ws63-rs` HAL 交叉验证。
