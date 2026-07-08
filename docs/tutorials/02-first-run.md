# 教程 2：跑通第一个固件

本教程接着 [教程 1：环境准备与安装](01-install.md)——假设你已经构建好仿真器，且当前在
`hisi-riscv-qemu` 目录里。现在我们**运行仓库内预构建固件**，在终端看到输出。

## 你将得到什么

一个真实的厂商 C SDK 固件（默认 `tests/csdk/dma.elf`）在仿真的 WS63 上启动，跑通 DMA 内存搬运样例并打印成功标志。
这证明仿真器、机器模型、xlinx 指令和串口输出链路都能工作。

## 第 1 步：运行默认固件

`scripts/run.sh` 不带参数时会运行仓库自带的预构建 C SDK fixture：

```bash
bash scripts/run.sh
```

终端应出现：

```text
dma memory copy test succ
```

看到这一行，说明**C SDK 固件 → DMA 模型 → 自定义 HiSilicon UART → `-serial` → 你的终端**这条链路全通了。

**退出 QEMU**：先按 `Ctrl-A`，松开，再按 `X`。

想看到 GPIO 翻转的痕迹，开追踪：

```bash
DEBUG=1 bash scripts/run.sh
#  随后查看 qemu.log，里面有中断/未建模访问等记录
```

## 第 2 步：换一个预构建固件

显式传入 ELF 路径即可运行其它 fixture。比如 ADC 样例：

```bash
bash scripts/run.sh tests/csdk/adc.elf
```

终端应打印：

```text
voltage: N mv
```

`N` 是样例读到的模拟电压值。想临时改无参默认固件，也可以用：

```bash
DEFAULT_ELF=tests/csdk/tcxo.elf bash scripts/run.sh
```

## 你做到了

你已经：在无硬件、无外部固件仓库、无 Rust 工具链的情况下跑通了真实固件，并看到了串口输出。

## 下一步

- 想系统地运行各种固件、开各种开关 → [运行固件](../how-to/run-firmware.md) 与 [运行选项](../reference/run-options.md)。
- 想重新生成 C SDK fixture → [运行固件 §运行 fbb_ws63 C SDK 固件](../how-to/run-firmware.md#运行-fbb_ws63-c-sdk-固件)。
- 想跑 Rust 示例固件 → [安装与构建仿真器 §准备固件](../how-to/install-and-build.md#准备固件)。
- 想调试 → [调试与追踪](../how-to/debug-and-trace.md)。
- 想跑回归测试 → [运行测试](../how-to/run-tests.md)。
- 想知道仿真器到底建模了什么、边界在哪 → [外设建模矩阵](../reference/peripheral-matrix.md) 与 [已知边界与非目标](../explanation/limitations.md)。
