# 运行选项参考

`scripts/run.sh` 与直接调用 QEMU 的选项清单。用法导向的讲解见 [运行固件](../how-to/run-firmware.md)。

## 调用形式

```bash
bash scripts/run.sh [firmware.elf] [额外 qemu 参数...]
```

- 不带 `firmware.elf` 时默认跑仓库内预构建 fixture：`tests/csdk/dma.elf`。
- 额外参数原样透传给 QEMU（如 `-s -S`）。

直接调用：

```bash
./qemu/build/qemu-system-riscv32 -M ws63 -nographic -serial mon:stdio -kernel firmware.elf
```

**退出 QEMU**：`Ctrl-A` 然后 `X`。

## 行为开关（环境变量）

| 变量 | 作用 |
|------|------|
| `DEBUG=1` | 加 `-d int,guest_errors,unimp -D qemu.log`，把中断/guest 错误/未建模访问写入 `qemu.log` |
| `ICOUNT=1` | **确定性指令计时**（`-icount shift=2`，约 250 MHz、IPC=1）：同一固件每次运行计时**完全一致**。**非**周期精确 |
| `ICOUNT_SHIFT=N` | 改 icount shift（默认 2→4 ns/insn≈250 MHz;3→125 MHz）|
| `NV=1` | 用分区表 + NV 镜像（`tests/csdk/flash/`）回填 flash XIP 窗口，使 C SDK 的分区/NV 读取成功 |
| `SEMIHOST=1` | 加 `-semihosting`：固件可用 RISC-V semihosting `SYS_EXIT` 设置 QEMU **进程退出码**（CI 免解析 UART 即得 pass/fail）、`SYS_WRITE0` 打印到控制台。见 ws63-rs `ws63-examples/semihost_selftest` |

## 路径变量

| 变量 | 作用 | 默认 |
|------|------|------|
| `QEMU_DIR` / `QEMU_BIN` | 仿真器位置 | `<repo>/qemu` |
| `DEFAULT_ELF` | 无参运行时使用的固件 ELF | `tests/csdk/dma.elf` |
| `FLASH_DIR` | NV overlay 目录 | `tests/csdk/flash/` |
| `QEMU_TAG` | 构建用的 QEMU tag | `v10.0.0` |
| `JOBS` | 构建并行度 | `nproc` |

## 常用 QEMU `-d` 标志

| 标志 | 打印 |
|------|------|
| `-d unimp` | 对未建模/兜底外设的访问（按地址定位）|
| `-d int` | 每次中断投递（IRQ 号/向量）|
| `-d guest_errors` | guest 侧错误访问 |
| `-d trace:ws63_gpio_*` | WS63 GPIO 输出变化 trace 事件 |
| `-trace ws63_dma_xfer` | WS63 DMA 搬运 trace 事件 |
| `-D <file>` | 把 `-d` 输出写入文件 |

追踪用法见 [调试与追踪](../how-to/debug-and-trace.md)。

## 示例

```bash
bash scripts/run.sh                         # 运行默认预构建 C SDK fixture
bash scripts/run.sh tests/csdk/adc.elf      # 显式运行另一个 fixture
DEFAULT_ELF=tests/csdk/tcxo.elf bash scripts/run.sh
ICOUNT=1 bash scripts/run.sh fw.elf          # 可复现计时
NV=1 bash scripts/run.sh ws63-liteos-app.elf # C SDK app + 分区表/NV
DEBUG=1 bash scripts/run.sh fw.elf           # 写 qemu.log 追踪
bash scripts/run.sh fw.elf -s -S             # 起 gdbstub 并冻结在复位
```

## QEMU 官方文档

这些开关多数透传给上游 QEMU，语义以官方文档为准：

- [Invocation](https://www.qemu.org/docs/master/system/invocation.html) —— 全部命令行选项（`-d`、`-serial`、`-semihosting`、`-icount` …）
- [Record/replay（`-icount`）](https://www.qemu.org/docs/master/system/replay.html) —— 确定性指令计时背后的机制
- [Emulation（Semihosting）](https://www.qemu.org/docs/master/about/emulation.html) —— `-semihosting` 与 `SYS_EXIT` 退出码
- [Tracing](https://www.qemu.org/docs/master/devel/tracing.html) —— `-trace` / `-d trace:` 事件追踪
