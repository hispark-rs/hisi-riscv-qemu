# 教程 2：跑通第一个固件

本教程接着 [教程 1：环境准备与安装](01-install.md)——假设你已经构建好仿真器、装好 `hisi-riscv` 工具链，
且当前在 `hisi-riscv-qemu` 目录里。现在我们**构建第一个固件并运行它**，在终端看到输出。

## 你将得到什么

一个真实的裸机固件（`blinky`）在仿真的 WS63 上启动、翻转 GPIO，整个过程**零非法指令陷阱**——证明你的
工具链、构建、内存映射全部正确。

## 第 1 步：构建 blinky 固件

我们用 `hisi-riscv-rs` 里的 `blinky` 示例：

```bash
cd ../hisi-riscv-rs
cargo build -p blinky --release
cd ../hisi-riscv-qemu
```

固件产物落在 `../hisi-riscv-rs/target/riscv32imfc-unknown-none-elf/release/blinky`。

## 第 2 步：运行它

```bash
bash scripts/run.sh
```

不带参数时，`run.sh` 默认就跑同级 `../hisi-riscv-rs` 里的 `blinky`。你会看到 QEMU 启动并运行固件。

**退出 QEMU**：先按 `Ctrl-A`，松开，再按 `X`。

想看到 GPIO 翻转的痕迹，开追踪：

```bash
DEBUG=1 bash scripts/run.sh
#  随后查看 qemu.log，里面有中断/未建模访问等记录
```

## 第 3 步：换一个会打印的固件

`blinky` 只翻 GPIO、不打印。换 `uart_hello` 就能在终端直接看到串口输出：

```bash
cd ../hisi-riscv-rs && cargo build -p uart_hello --release && cd ../hisi-riscv-qemu
bash scripts/run.sh ../hisi-riscv-rs/target/riscv32imfc-unknown-none-elf/release/uart_hello
```

终端应打印：

```text
Hello from WS63 on QEMU!
```

看到这一行，说明**自定义 HiSilicon UART → `-serial` → 你的终端**这条链路全通了。

## 你做到了

你已经：构建了真实裸机固件，并在无硬件下跑通了它，还看到了串口输出。

## 下一步

- 想系统地运行各种固件、开各种开关 → [运行固件](../how-to/run-firmware.md) 与 [运行选项](../reference/run-options.md)。
- **想跑 C 固件**（厂商 fbb_ws63 C SDK，非 Rust）→ [运行固件 §运行 fbb_ws63 C SDK 固件](../how-to/run-firmware.md#运行-fbb_ws63-c-sdk-固件)；固件由 [`fbb_ws63-qemu`](https://github.com/hispark-rs/fbb_ws63-qemu) 构建。
- 想调试 → [调试与追踪](../how-to/debug-and-trace.md)。
- 想跑回归测试 → [运行测试](../how-to/run-tests.md)。
- 想知道仿真器到底建模了什么、边界在哪 → [外设建模矩阵](../reference/peripheral-matrix.md) 与 [已知边界与非目标](../explanation/limitations.md)。
