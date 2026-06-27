# 安装与构建仿真器

本指南覆盖两条获得 `qemu-system-riscv32 -M ws63` 的路径：**下载预编译 Release**（最快）或**从源码构建**（可移植）。
任选其一。第一次上手建议直接走 [快速上手教程](../tutorials/getting-started.md)。

## 系统要求

- **OS**：Linux（主要在 Ubuntu/Debian x86_64 上验证）；其他平台请走「从源码构建」。
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
3. 应用该版本的 patch-series `patches/$QEMU_TAG/0001..*.patch`（对既有 QEMU 文件的改动，见 [patch-series 参考](../../patches/README.md)）；
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

仿真器本身不含固件，你需要一个 **ELF** 来跑。两条路径：

**(a) ws63-rs（Rust 裸机）** —— 需要 `hisi-riscv` 自定义 Rust 工具链（rv32imfc 硬浮点、无原子，内置为 builtin target）：

```bash
curl -fLO https://github.com/hispark-rs/hisi-riscv-rust-toolchain/releases/download/v1.96.0-2/hisi-riscv-rust-1.96.0-x86_64-unknown-linux-gnu.tar.gz
tar xzf hisi-riscv-rust-1.96.0-*.tar.gz && rustup toolchain link hisi-riscv "$PWD/stage2"
# 在 ws63-rs 仓库中:
cargo build -p blinky --release
#   产物:target/riscv32imfc-unknown-none-elf/release/blinky
```

**(b) fbb_ws63 C SDK（厂商 gcc）** —— 用 SDK 内置工具链：

```bash
cd fbb_ws63/src && python3 build.py ws63-liteos-app -ninja
```

> 跑 C SDK app **不需要**自己装工具链来用仓库自带的测试 fixture——`tests/csdk/` 里已有预编译样例
> ELF，见 [运行测试 §C SDK 样例](run-tests.md#c-sdk-外设样例)。

## QEMU 官方文档

本项目是上游 QEMU 的 fork，构建/运行的通用知识以 QEMU 官方文档为准：

- [QEMU 文档主页](https://www.qemu.org/docs/master/)
- [构建系统（meson/ninja 依赖与流程）](https://www.qemu.org/docs/master/devel/build-system.html)
- [RISC-V 系统仿真](https://www.qemu.org/docs/master/system/target-riscv.html) —— 上游的 RISC-V 机器与 CPU 概览（本项目在其上加 `-M ws63`）

## 相关

- 跑起来之后怎么用 → [运行固件](run-firmware.md)
- QEMU 版本与 patch-series → [移植到新的 QEMU 版本](port-qemu-version.md)、[patch-series 参考](../../patches/README.md)
- 为什么是 fork QEMU 而非树外插件 → [设计取舍](../explanation/design-rationale.md)
