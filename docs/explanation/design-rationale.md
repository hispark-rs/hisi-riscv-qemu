# 设计取舍：为什么 fork QEMU

本文讨论 ws63-qemu 的几个核心设计决策**为什么这么做**——目标、为何 fork 而非树外插件、为何不用 `-M virt`、
以及为何按 QEMU 版本维护 patch-series。要查机器组件的事实清单见 [机器模型](../reference/machine-model.md)。

## 目标

在 QEMU 上仿真 HiSilicon WS63（RISC-V）SoC，让 [`ws63-rs`](https://github.com/hispark-rs/hisi-riscv-rs) 裸机固件
无需真实硬件即可运行——为 ROADMAP 阶段 1「硬件在环 bring-up」提供一个**软件在环**替代信号：证明内存布局、
startup（PMP/FPU/cache/栈/数据重定位）、链接脚本在一个 WS63 地址空间模型上能正确跑起来。

它是 **软件在环（SIL）** 的驱动验证底座，**不是**周期精确的微架构模拟器。

## 方法：fork 一个固定版本的 QEMU（仿 esp-qemu）

QEMU 没有稳定的「树外板卡插件」ABI，因此自定义 SoC 的标准做法是 fork 并加一个 in-tree 板卡文件
——Espressif 的 esp-qemu 即如此（`hw/riscv/esp32c3.c` + 自定义 CPU/外设）。本项目同样：

- 默认基线 **QEMU v10.0.0**（`scripts/build.sh` 克隆该 tag；从 v9.2.4 升级而来）。**同时维护 v10.2.3、v11.0.1 与 v9.2.4**。
- 新增单文件 `hw/riscv/ws63.c`（机器模型 + 外设设备）+ `insn_trans/trans_xlinx.c.inc`（xlinx 解码器）+
  `tests/qtest/ws63-test.c` 作为**新文件直接拷入**（不冲突，保留在 `src/` 便于编辑）；对**既有 QEMU 文件的改动**
  走**按版本分目录的 patch-series**（见 [patch-series 参考](../../patches/README.md)）。
- 只构建 `riscv32-softmmu` 一个目标，控制构建时间（~10–20 分钟）。

### 为什么不用树外补丁

board 没有稳定插件 ABI——树外加载自定义机器没有受支持的途径，所以必须 fork 进树。

### 为什么不用 `-M virt`

固件按 WS63 地址链接（外设基址、内存布局都是 WS63 特定的），首次访问 WS63 外设就会在 `virt` 上 fault——
`virt` 是一个通用虚拟机器，其地址布局与 WS63 完全不同。

## 为什么按 QEMU 版本维护 patch-series

对既有 QEMU 文件的改动会随版本漂移（头文件搬家、结构体/字段偏移变化、惯用法变更）。把这些改动凝固进单一补丁
会在每次 QEMU 升级时全线冲突。**按版本分目录**（`patches/<tag>/`）让每条序列对其基线干净可套，升级是「新建一个
目录」而非「改一份会到处冲突的补丁」。

漂移有多真实：10.0→10.2 就移动了 `insn_len`、把 CPU 定义改成声明式 `DEFINE_RISCV_CPU`、把 `decode_opc` 改成表
驱动、把 `CharBackend` 改名 `CharFrontend`；10.2→11 又把六个 `hw/*.h` 迁到 `hw/core/*.h`。移植流程见
[移植到新的 QEMU 版本](../how-to/port-qemu-version.md)。

## 「可选确定性指令计时」而非周期精确

TCG 不模拟流水线/cache/逐指令周期，默认虚拟时间自由运行（每次运行计时随宿主时钟漂移）。`ICOUNT=1`
（= `-icount shift=2`，约 250 MHz、IPC=1）把虚拟时间绑定指令数，**同一固件每次运行结果完全一致**（实测 1e6
循环 3 次均 = 2,880,003 ticks）。这是 IPC=1 近似，**不是**真实微架构周期精确——真周期级需 gem5 等，非本仿真器
目标。详见 [已知边界与非目标](limitations.md)。

## 多角度交叉验证

仿真器既跑 ws63-rs（Rust，标准 rv32imfc），也跑 fbb_ws63 **C SDK** 厂商 gcc 编译的固件（需实现 xlinx 自定义 ISA）。
两侧固件在同一外设模型上对照，可交叉验证内存映射、启动、外设寄存器时序与驱动逻辑——用厂商实现反向核验 rs HAL。
方法与结论见 [多角度对齐分析](alignment-analysis.md)。

## 相关

- 机器组件、UART 寄存器、中断 CSR → [机器模型](../reference/machine-model.md)
- 中断投递机制 → [中断控制器设计](interrupt-controller.md)
- patch-series 结构 → [patch-series 参考](../../patches/README.md)
- 边界与非目标 → [已知边界与非目标](limitations.md)
- 上游 RISC-V 系统仿真（fork 的基线）→ QEMU 官方 [RISC-V System emulation](https://www.qemu.org/docs/master/system/target-riscv.html)
