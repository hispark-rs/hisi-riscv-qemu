# 教程 1：环境准备与安装

本教程带你**第一次**把环境装好：克隆两个仓库、装系统依赖、从源码构建仿真器、装好 Rust 工具链。
跑完后你就**万事俱备**，可以进入 [教程 2：跑通第一个固件](02-first-run.md)。

跟着每一步做即可，**不需要预先理解内部原理**。本教程只给「最小够用」的引导式安装；**完整的安装选项**
（下载预编译 Release、依赖明细表、各平台差异）见操作指南 [安装与构建仿真器](../how-to/install-and-build.md)。

> 适用平台：本教程在 **Ubuntu/Debian x86_64** 上验证。其他平台也能构建，详见上面的操作指南。
> 你**不需要** WS63 硬件（EVB）。

## 前置

- **rustup**：第 4 步要用 Rust 工具链，请先装好 [rustup](https://rustup.rs/)（提供 `rustup` / `cargo`）。
- **sudo**：第 2 步装系统依赖需要。

## 第 1 步：克隆两个仓库

仿真器仓库 `hisi-riscv-qemu` 与固件仓库 `hisi-riscv-rs`（提供示例固件）需要是相邻的两个目录：

```bash
git clone https://github.com/hispark-rs/hisi-riscv-qemu.git
git clone https://github.com/hispark-rs/hisi-riscv-rs.git
cd hisi-riscv-qemu
```

> 并排克隆后，脚本默认即从同级的 `../hisi-riscv-rs` 找固件，后续命令无需额外设环境变量。

## 第 2 步：安装系统依赖

```bash
bash scripts/setup-deps.sh
```

它会用 `apt-get` 装上 git / build-essential / pkg-config / ninja / meson / glib / pixman / flex / bison /
python3 / libslirp-dev 等。需要 `sudo`。

> 各依赖的作用、非 Debian 平台（macOS brew / Windows MSYS2）的等价包，见
> [安装与构建仿真器 §构建依赖明细](../how-to/install-and-build.md#构建依赖明细)。

## 第 3 步：构建仿真器

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

## 第 4 步：安装 Rust 工具链

示例固件需要 `hisi-riscv` 自定义 Rust 工具链（rv32imfc 硬浮点、无原子，内置为 builtin target）：

```bash
curl -fLO https://github.com/hispark-rs/hisi-riscv-rust-toolchain/releases/download/v1.96.0-2/hisi-riscv-rust-1.96.0-x86_64-unknown-linux-gnu.tar.gz
tar xzf hisi-riscv-rust-1.96.0-*.tar.gz
rustup toolchain link hisi-riscv "$PWD/stage2"
```

`rustup toolchain link` 把解压出的 `stage2` 注册为名为 `hisi-riscv` 的工具链——`hisi-riscv-rs` 的
`rust-toolchain.toml` 会自动选用它。

## 你装好了

现在你有：从源码构建的、带 WS63 机器的 QEMU + 可编译 WS63 固件的 Rust 工具链。

## 下一步

→ [教程 2：跑通第一个固件](02-first-run.md) —— 构建 blinky、运行它、看到输出。

想了解更完整的安装方式（预编译 Release、各平台、依赖表）→ [安装与构建仿真器](../how-to/install-and-build.md)。
