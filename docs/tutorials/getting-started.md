# 快速上手：从零跑通第一个固件

本教程带你**第一次**把 ws63-qemu 跑起来：从源码构建仿真器，准备一个固件，运行它，并在终端看到输出。
全程约 15–25 分钟（大头是首次构建 QEMU）。跟着每一步做即可，**不需要预先理解内部原理**——理解可以以后再看
[解释](../explanation/design-rationale.md)。

> 适用平台：本教程在 **Ubuntu/Debian x86_64** 上验证。其他平台也能构建，但本教程只走这条最顺的路。
> 你**不需要** WS63 硬件（EVB）。

## 你将得到什么

跑完本教程，你会在终端看到一个真实的裸机固件（`blinky`）在仿真的 WS63 上启动、翻转 GPIO，整个过程
**零非法指令陷阱**——证明你的工具链、构建、内存映射全部正确。

## 第 0 步：准备目录

本教程假设仿真器仓库 `hisi-riscv-qemu` 与固件仓库 `hisi-riscv-rs`（提供示例固件）是相邻的两个目录：

```bash
git clone https://github.com/hispark-rs/hisi-riscv-qemu.git
git clone https://github.com/hispark-rs/hisi-riscv-rs.git
cd hisi-riscv-qemu
```

> 并排克隆后，脚本默认即从同级的 `../hisi-riscv-rs` 找固件，后续命令无需额外设环境变量。
> 第 3 步要用 Rust：请先装好 [rustup](https://rustup.rs/)（提供 `rustup` / `cargo`）。

## 第 1 步：安装构建依赖

```bash
bash scripts/setup-deps.sh
```

它会用 `apt-get` 装上 git / build-essential / pkg-config / ninja / meson / glib / pixman / flex / bison /
python3 / libslirp-dev 等。需要 `sudo`。

## 第 2 步：构建仿真器

```bash
bash scripts/build.sh
```

这一步会浅克隆固定版 QEMU（默认 `v10.0.0`）到 `./qemu/`，注入 WS63 板卡文件、应用 patch-series，然后只构建
`riscv32-softmmu` 单目标。**首次约 10–20 分钟**（取决于核数）。产物是 `./qemu/build/qemu-system-riscv32`。

构建完成后，确认机器已注册：

```bash
./qemu/build/qemu-system-riscv32 -M help | grep ws63
```

应输出含 `ws63` 的一行。看到了就说明仿真器侧准备好了。

## 第 3 步：准备一个固件

我们用 `hisi-riscv-rs` 里的 `blinky` 示例。它需要 `hisi-riscv` 自定义 Rust 工具链（rv32imfc 硬浮点、无原子）：

```bash
curl -fLO https://github.com/hispark-rs/hisi-riscv-rust-toolchain/releases/download/v1.96.0-2/hisi-riscv-rust-1.96.0-x86_64-unknown-linux-gnu.tar.gz
tar xzf hisi-riscv-rust-1.96.0-*.tar.gz
rustup toolchain link hisi-riscv "$PWD/stage2"

cd ../hisi-riscv-rs
cargo build -p blinky --release
cd ../hisi-riscv-qemu
```

固件产物落在 `../hisi-riscv-rs/target/riscv32imfc-unknown-none-elf/release/blinky`。

## 第 4 步：运行它

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

## 第 5 步：换一个会打印的固件

`blinky` 只翻 GPIO、不打印。换 `uart_hello` 就能在终端直接看到串口输出：

```bash
cd ../hisi-riscv-rs && cargo build -p uart_hello --release && cd ../hisi-riscv-qemu
bash scripts/run.sh ../hisi-riscv-rs/target/riscv32imfc-unknown-none-elf/release/uart_hello
```

终端应打印：

```
Hello from WS63 on QEMU!
```

看到这一行，说明**自定义 HiSilicon UART → `-serial` → 你的终端**这条链路全通了。

## 你做到了

你已经：从源码构建了带 WS63 机器的 QEMU、构建了真实裸机固件、并在无硬件下跑通了它。

## 下一步

- 想系统地运行各种固件、开各种开关 → [运行固件](../how-to/run-firmware.md) 与 [运行选项](../reference/run-options.md)。
- 想调试 → [调试与追踪](../how-to/debug-and-trace.md)。
- 想跑回归测试 → [运行测试](../how-to/run-tests.md)。
- 想知道仿真器到底建模了什么、边界在哪 → [外设建模矩阵](../reference/peripheral-matrix.md) 与 [已知边界与非目标](../explanation/limitations.md)。
