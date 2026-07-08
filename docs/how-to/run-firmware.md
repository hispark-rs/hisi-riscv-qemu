# 运行固件

本指南讲怎么在 ws63-qemu 上运行一个固件 ELF——包括基本运行、`run.sh` 各开关、仓库自带预构建 fixture，以及运行
fbb_ws63 C SDK 固件（含用 `NV=1` 回填分区表/NV）。`run.sh` 全部选项的清单见
[运行选项参考](../reference/run-options.md)。

## 基本运行

```bash
# 用包装脚本（推荐）:不带参数默认跑 tests/csdk/dma.elf
bash scripts/run.sh
bash scripts/run.sh path/to/firmware.elf
DEFAULT_ELF=tests/csdk/tcxo.elf bash scripts/run.sh

# 直接调用 QEMU
./qemu/build/qemu-system-riscv32 -M ws63 -nographic -serial mon:stdio -kernel firmware.elf
```

**退出 QEMU**：`Ctrl-A` 然后 `X`。

串口约定：`-serial mon:stdio` 把 **UART0** 的 TX 输出到你的终端，终端输入送入 UART0 的 RX（中断使能时触发
IRQ 53）。`mon:` 表示同一通道复用 QEMU monitor（`Ctrl-A C` 切换）。

## 常用开关

通过环境变量开启（`run.sh <elf> [额外 qemu 参数...]`，额外参数原样透传给 QEMU）：

```bash
ICOUNT=1 bash scripts/run.sh fw.elf          # 可复现的确定性指令计时（IPC=1 近似，非周期精确）
NV=1 bash scripts/run.sh ws63-liteos-app.elf # C SDK app + 分区表/NV 回填
DEBUG=1 bash scripts/run.sh fw.elf           # 写 qemu.log 追踪（见调试与追踪指南）
SEMIHOST=1 bash scripts/run.sh selftest.elf  # 固件用 semihosting SYS_EXIT 设置 QEMU 退出码
```

各变量完整语义见 [运行选项参考](../reference/run-options.md)。`DEBUG`/trace 相关用法见 [调试与追踪](debug-and-trace.md)。

## 运行仓库自带 fixture

`tests/csdk/` 里有预构建的 fbb_ws63 C SDK 外设样例。它们是当前文档 happy path，不需要相邻的
`hisi-riscv-rs` checkout，也不需要安装 Rust 或厂商 SDK 工具链。

```bash
bash scripts/run.sh                 # 默认 tests/csdk/dma.elf
bash scripts/run.sh tests/csdk/adc.elf
DEFAULT_ELF=tests/csdk/tcxo.elf bash scripts/run.sh
```

默认 `dma.elf` 的成功标志是：

```text
dma memory copy test succ
```

批量断言这些 fixture 用 [运行测试](run-tests.md) 里的 `scripts/csdk-test.sh`。

## 运行 fbb_ws63 C SDK 固件

仿真器能跑 fbb_ws63 **C SDK** 厂商 gcc 编译的固件（依赖 xlinx 自定义 ISA，已实现）。这类固件可由
[`hispark-rs/fbb_ws63-qemu`](https://github.com/hispark-rs/fbb_ws63-qemu) 构建——
它是 fbb_ws63 C SDK 的 **QEMU 适配 fork**，已为本仿真器预裁剪（注释掉 BT/WiFi 任务以干净启动），产出可直接 boot 的
固件。详见其 [`README-QEMU.md`](https://github.com/hispark-rs/fbb_ws63-qemu/blob/master/README-QEMU.md)。

```bash
# 在 fbb_ws63-qemu 仓库中构建（厂商工具链已内置）
cd src && python3 build.py ws63-liteos-app -c -ninja
#   产物:output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf

# 运行
qemu-system-riscv32 -M ws63 -nographic -serial mon:stdio -kernel output/ws63/acore/ws63-liteos-app/ws63-liteos-app.elf
```

`flashboot` 会跑出时钟 bring-up → flash init 的 UART 输出；`ws63-liteos-app`（裁剪 BT+WiFi）会稳定启动到
`cpu 0 entering scheduler` 并空转运行。

### 用 `NV=1` 回填分区表 + NV

`-kernel` 启动直接把 app 装入 RAM、**跳过 flashboot**（真机里 flashboot 负责把分区表/NV 写进 flash），所以
flash XIP 窗口（`0x200000`，RAM 背靠）默认是空的，C SDK 的 `uapi_partition_get_info()` / NV 读取会失败
（`[UPG] ...flash_start_addr fail`、`nv read sw fail`）。

**解法**：`NV=1` 用 `-device loader` 把分区表 + NV 回填进 flash：

```bash
NV=1 bash scripts/run.sh ws63-liteos-app.elf
```

回填的镜像（`tests/csdk/flash/`，见其 `manifest.txt`）：

| 镜像 | XIP 地址 | 内容 |
|------|----------|------|
| `partition_params.bin` | `0x200000` | params 区，分区表在 `0x200380`（magic `0x4b87a54b`）|
| `nv.bin` | `0x5FC000` | 软件 NV 键值区 |
| `nv_factory.bin` | `0x20C000` | 出厂 NV 键值区 |

三者都落在 app 自身 XIP 区（~`0x230300`–`0x294000`）**之外**，故不冲突。回填后分区表解析成功、NV 读取成功，
UPG 的 `flash_start_addr fail` 消除。

> **唯一残留**：`xo_trim`（晶振温补标定）等**逐芯片出厂标定键**——生产时烧录，任何构建产物的 NV 都不含，
> 固有缺失、非缺陷。详见 [已知边界与非目标](../explanation/limitations.md)。

### 含 BT/WiFi 的 app

含 BT/WiFi 任务的 C SDK app 会在子系统深层初始化崩于 **ROM 数据墙**（vtable/NV/efuse/RF 标定无 dump），
需在 `config.py` 注释 `BGLE/BTH/WIFI_TASK_EXIST` 裁剪这些任务。原因见 [已知边界与非目标](../explanation/limitations.md)。

## 故障排查

| 现象 | 原因 / 解法 |
|------|-------------|
| `QEMU not built: .../qemu-system-riscv32` | 先 `bash scripts/build.sh`（见 [安装与构建](install-and-build.md)）|
| `firmware ELF not found` | `run.sh` 第一个参数给对 ELF 路径；若是无参运行，确认 `tests/csdk/dma.elf` 存在或用 `DEFAULT_ELF=...` 覆盖 |
| `-M help` 不含 ws63 | 构建未注入成功；删 `qemu/` 重跑 `build.sh` |
| 固件一上来就 **illegal instruction** 大量陷阱 | 多为 xlinx ISA 或 ROM 调用问题；`DEBUG=1` 看 `qemu.log` 的 pc 落点，ROM 区（`0x109xxx`）是预期被拦截的 |
| C SDK app 反复打印 `[UPG] ...flash_start_addr fail` / `nv read sw fail` | 用 **`NV=1`** 回填；残留的 `xo_trim` 一行是固有的 |
| C SDK BT/WiFi 任务崩溃 | 预期（ROM 数据墙）；裁剪 `BGLE/BTH/WIFI_TASK_EXIST` 任务 |
| 计时每次运行都不同 | 默认虚拟时间自由运行；要可复现用 **`ICOUNT=1`** |

更完整的语义边界与 FAQ 见 [已知边界与非目标](../explanation/limitations.md)。

## 相关

- 全部开关与命令行 → [运行选项参考](../reference/run-options.md)
- 调试某次运行 → [调试与追踪](debug-and-trace.md)
- 验证一批固件 → [运行测试](run-tests.md)
