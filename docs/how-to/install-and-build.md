# 安装与构建仿真器

本指南覆盖两条获得 `qemu-system-riscv32 -M ws63` 的路径：**下载预编译 Release**（最快）或**从源码构建**（可移植）。
任选其一。第一次上手建议直接走教程 [环境准备与安装](../tutorials/01-install.md) → [跑通第一个固件](../tutorials/02-first-run.md)。

## 系统要求

- **OS**：Linux（主要在 Ubuntu/Debian x86_64 上验证）；其他平台请走「从源码构建」。
- **默认测试范围**：happy path 只覆盖仍处于官方维护周期内的 Ubuntu/Debian。Ubuntu 以标准维护期为准，
  不含 Ubuntu Pro / ESM / Legacy add-on 等延长服务；Debian 不含 ELTS 等延长支持。
- **磁盘**：QEMU 源码树 + 构建产物约 **2 GB**。
- **首次构建耗时**：约 **10–20 分钟**（`build.sh` 只构建 `riscv32-softmmu` 单目标）。
- **运行时**：仅需 `qemu-system-riscv32` 二进制（动态依赖 glib/pixman）。

## 方式 A：下载预编译 Release（最快）

从 [Releases](https://github.com/hispark-rs/hisi-riscv-qemu/releases) 下载对应版本资产：

| 资产 | 说明 |
|------|------|
| `qemu-system-riscv32-ws63-<ver>` | Ubuntu 构建的仿真器二进制（动态链接 glibc/glib/pixman）|
| `ws63-qemu-src-<ver>.tar.gz` | 源码包（`src/` `patches/` `scripts/` `tests/` `docs/` 等）——其他平台用它重建 |
| `SHA256SUMS` | 校验和 |

```bash
sha256sum -c SHA256SUMS
chmod +x qemu-system-riscv32-ws63-<ver>
./qemu-system-riscv32-ws63-<ver> -M help | grep ws63   # 确认机器已注册
```

> 预编译二进制是在 **Ubuntu** 上链接的。若你的发行版 glibc/glib/pixman 版本不兼容（`error while loading
> shared libraries`），请改用方式 B 从源码构建。

## 方式 B：从源码构建（可移植）

```bash
# 1. 安装构建依赖（Debian/Ubuntu；需 sudo）
bash scripts/setup-deps.sh
#   等价手动:apt-get install git build-essential pkg-config ninja-build meson \
#             libglib2.0-dev libpixman-1-dev flex bison python3 python3-venv zlib1g-dev libslirp-dev

# 2. 克隆固定版 QEMU、注入 WS63 板卡 + xlinx ISA、构建
bash scripts/build.sh
#   产物:./qemu/build/qemu-system-riscv32
```

`build.sh` 做的事（**幂等**，可反复运行做增量构建）：

1. 浅克隆 QEMU `$QEMU_TAG`（默认 `v10.0.0`，可改）到 `./qemu/`；若无 `patches/$QEMU_TAG/` 则报错列出已支持版本；
2. 拷入新文件：板卡源 `src/hw/riscv/ws63.c`、xlinx 解码器 `trans_xlinx.c.inc`、qtest `ws63-test.c`；
3. 应用该版本的 patch-series `patches/$QEMU_TAG/0001..*.patch`（对既有 QEMU 文件的改动，见 [patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)）；
4. `./configure --target-list=riscv32-softmmu --enable-slirp` 后 `make`。

构建相关环境变量：`QEMU_TAG`（默认 `v10.0.0`；另维护 `v10.2.3`、`v11.0.1`、`v9.2.4`）、`QEMU_DIR`（默认
`<repo>/qemu`）、`QEMU_REPO`、`JOBS`（默认 `nproc`）。

### 构建依赖明细

`scripts/setup-deps.sh` 会**自动识别宿主系统**并用对应包管理器安装。下表是 QEMU 构建链各依赖的作用
（Debian/Ubuntu 包名；其它平台的等价包由脚本按平台映射）：

| 依赖 | 作用 | 缺失时的症状 |
|------|------|--------------|
| `git` | 浅克隆固定版 QEMU 源码树 | `build.sh` 无法 clone |
| `build-essential`（gcc/g++/make）| C11 编译器 + `make` 驱动 | `configure` 报「no working compiler」|
| `pkg-config` | 定位 glib/pixman/slirp 的头与库 | `configure` 找不到 glib |
| `meson` + `ninja-build` | QEMU 的构建系统（meson 配置 → ninja 编译）| `configure` 报 meson/ninja 版本过低或缺失 |
| `libglib2.0-dev` | QEMU 核心依赖（GLib，对象/事件/数据结构）| `configure` 致命错误：glib 必需 |
| `libpixman-1-dev` | 像素操作库（显示后端）| 图形相关目标无法构建 |
| `libslirp-dev` | 用户态网络栈，启用 `--enable-slirp` → `-nic user` | 连接性示例（net_ping）的 SLIRP 后端不可用 |
| `flex` + `bison` | 构建期代码生成（词法/语法）| 部分生成步骤失败 |
| `python3` + `python3-venv` | QEMU 配置期建 venv（`mkvenv`）跑构建脚本 | `configure` 报「found no usable distlib / venv」|
| `zlib1g-dev` | 压缩库（镜像/迁移等）| 链接期缺 `-lz` |
| `patchelf` | Release 打包阶段修正二进制 rpath（仅打包用）| 仅影响 release 工件，不影响本地构建 |

> **磁盘/耗时再次提醒**：QEMU 源码树 + 构建产物约 **2 GB**，首次构建约 **10–20 分钟**。

### 各平台

`setup-deps.sh` 已覆盖三类宿主，自动选择包管理器（无需手动指定）：

| 平台 | 包管理器 | 备注 |
|------|----------|------|
| Linux（Debian/Ubuntu，x86_64 / aarch64）| `apt-get` | 主验证平台；需 `sudo` |
| macOS | Homebrew（`brew`）| 装 `meson ninja pkgconf glib pixman libslirp dylibbundler`；脚本另为 QEMU 的 `mkvenv` 补 `distlib` |
| Windows（MSYS2 / MINGW64）| `pacman` | 在 MSYS2 shell 内运行；CI 用 `msys2/setup-msys2` |

其它发行版请手动安装上表依赖的等价包（参见 QEMU 官方构建说明，见文末「QEMU 官方文档」）。

**验证安装**：

```bash
./qemu/build/qemu-system-riscv32 -M help | grep ws63   # 应输出含 "ws63" 的一行
```

## 准备固件

仿真器运行任意 WS63 ELF。入门和测试不需要另准备固件：仓库已经带了预构建 fixture；自行构建 C SDK 或 Rust 固件是进阶路径。

**(a) 仓库自带预构建 C SDK fixture** —— `tests/csdk/` 里提交了 fbb_ws63 C SDK 外设样例 ELF，可直接运行：

```bash
bash scripts/run.sh                 # 默认 tests/csdk/dma.elf
bash scripts/run.sh tests/csdk/adc.elf
DEFAULT_ELF=tests/csdk/tcxo.elf bash scripts/run.sh
```

这些 fixture 由 `scripts/csdk-test.sh` 在 CI 中使用，适合做 happy path 和外设模型回归。

**(b) fbb_ws63 C SDK（厂商 gcc）** —— 用 [`hispark-rs/fbb_ws63-qemu`](https://github.com/hispark-rs/fbb_ws63-qemu)
（fbb_ws63 C SDK 的 QEMU 适配 fork，已为本仿真器预裁剪 BT/WiFi），SDK 内置工具链：

```bash
# 在 fbb_ws63-qemu 仓库中
cd src && python3 build.py ws63-liteos-app -c -ninja
#   产物:output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf
```

> 重新生成本仓库测试 fixture：`scripts/build-csdk-samples.sh`（从 fbb_ws63 checkout 选一个
> `CONFIG_SAMPLE_SUPPORT_*`、干净构建、strip 到约 400 KB）。

**(c) ws63-rs（Rust 裸机）** —— 需要 `hisi-riscv` 自定义 Rust 工具链（rv32imfc 硬浮点、无原子，内置为 builtin
target）。**先装好基础 Rust 环境（[rustup](https://rustup.rs/)）**——本工具链以 rustup 工具链形式安装，依赖 rustup；
若已装过可跳过。安装方式与上游 hisi-riscv-rs 一致（解压进 rustup 的 toolchains 目录，自动识别，无需 `toolchain link`）：

```bash
HOST=x86_64-unknown-linux-gnu   # 或 aarch64-unknown-linux-gnu / aarch64-apple-darwin / x86_64-pc-windows-msvc
curl -LO https://github.com/hispark-rs/hisi-riscv-rust-toolchain/releases/download/v1.96.0-2/hisi-riscv-rust-1.96.0-$HOST.tar.gz
mkdir -p ~/.rustup/toolchains/hisi-riscv
tar xzf hisi-riscv-rust-1.96.0-$HOST.tar.gz --strip-components=1 -C ~/.rustup/toolchains/hisi-riscv
rustup toolchain list | grep hisi-riscv   # 验证:应输出 hisi-riscv
# 在 ws63-rs 仓库中:
cargo build -p blinky --release
#   产物:target/riscv32imfc-unknown-none-elf/release/blinky
```

> 工具链安装的权威步骤与各平台细节以上游为准：
> [hisi-riscv-rs · 安装 hisi-riscv 工具链](https://hispark-rs.github.io/hisi-riscv-rs/tutorials/app/01-setup.html#第-1-步安装-hisi-riscv-工具链)。

## QEMU 官方文档

本项目是上游 QEMU 的 fork，构建/运行的通用知识以 QEMU 官方文档为准：

- [QEMU 文档主页](https://www.qemu.org/docs/master/)
- [构建系统（meson/ninja 依赖与流程）](https://www.qemu.org/docs/master/devel/build-system.html)
- [RISC-V 系统仿真](https://www.qemu.org/docs/master/system/target-riscv.html) —— 上游的 RISC-V 机器与 CPU 概览（本项目在其上加 `-M ws63`）

## 相关

- 跑起来之后怎么用 → [运行固件](run-firmware.md)
- QEMU 版本与 patch-series → [移植到新的 QEMU 版本](port-qemu-version.md)、[patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)
- 为什么是 fork QEMU 而非树外插件 → [设计取舍](../explanation/design-rationale.md)
