# 教程 1：环境准备与安装

本教程带你**第一次**把环境装好：克隆本仓库、装系统依赖、从源码构建仿真器。
跑完后你就**万事俱备**，可以进入 [教程 2：跑通第一个固件](02-first-run.md)。

跟着每一步做即可，**不需要预先理解内部原理**。本教程只给「最小够用」的引导式安装；**完整的安装选项**
（下载预编译 Release、依赖明细表、各平台差异）见操作指南 [安装与构建仿真器](../how-to/install-and-build.md)。

> 适用平台：本教程在 **Ubuntu/Debian x86_64** 上验证。其他平台也能构建，详见上面的操作指南。
> 你**不需要** WS63 硬件（EVB）。

## 前置

- **git**：第 1 步需要用 `git clone` 拉取本仓库。若系统还没有 git，请先用系统包管理器安装（如
  `sudo apt-get install git`）。
- **sudo**：第 2 步装系统依赖需要。

## 第 1 步：克隆仓库

入门路径只需要仿真器仓库；可运行的预构建 C SDK 固件已经提交在 `tests/csdk/`。

```bash
git clone https://github.com/hispark-rs/hisi-riscv-qemu.git
cd hisi-riscv-qemu
```

## 第 2 步：安装系统依赖

```bash
bash scripts/setup-deps.sh
```

它会用 `apt-get` 装上 git / build-essential / pkg-config / ninja / meson / glib / pixman / flex / bison /
python3 / libslirp-dev 等。需要 `sudo`。

> 各依赖的作用、非 Debian 平台（macOS brew / Windows MSYS2）的等价包，见
> [安装与构建仿真器 §构建依赖明细](../how-to/install-and-build.md#构建依赖明细)。

## 第 3 步：构建仿真器

> **想跳过这 10–20 分钟的构建？** Ubuntu 上可直接下载**预编译二进制**、解压即用，无需装构建依赖（第 2 步）
> 也无需源码构建：见 [安装与构建仿真器 §方式 A：下载预编译 Release](../how-to/install-and-build.md#方式-a下载预编译-release最快)。
> 本教程走源码构建（可移植、任意平台）；拿到 `qemu-system-riscv32` 后即可进入下一篇教程。

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

## 你装好了

现在你有：从源码构建的、带 WS63 机器的 QEMU，以及仓库内可直接运行的预构建固件。

## 下一步

→ [教程 2：跑通第一个固件](02-first-run.md) —— 运行预构建固件、看到串口输出。

想了解更完整的安装方式（预编译 Release、各平台、依赖表）→ [安装与构建仿真器](../how-to/install-and-build.md)。
