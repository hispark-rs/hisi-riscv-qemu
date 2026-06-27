# 已知边界与非目标

本文解释用 ws63-qemu 时**必须知道的语义边界**——它们大多不是缺陷，而是仿真器目标与物理/数据现实的边界。
不理解会误判结果。哪些外设建模到什么程度的事实表见 [验证覆盖范围](../reference/verification.md) 与
[外设建模矩阵](../reference/peripheral-matrix.md)。

## 语义边界（用之前必读）

### 1. 默认非周期精确，虚拟时间自由运行

TCG 不模拟流水线/cache/逐指令周期。需要可复现计时就用 `ICOUNT=1`，但那是 **IPC=1 近似**（≈250 MHz），
**不是**真实微架构周期。真周期级请用 gem5 等，非本仿真器目标。这条取舍的背景见 [设计取舍](design-rationale.md)。

### 2. 掩膜 ROM 是桩的

WS63 应用核有一块片上掩膜 ROM（`0x109000..0x14C000`，厂商不发布二进制）。仿真器在 `ws63_rom_call` 里
**仿真**用到的 ROM 函数，**未识别的 ROM 地址返回 0（成功）**让固件不崩。代价：依赖 **ROM 数据**（非代码）的
功能无法重建——见下「ROM 数据墙」。清单见 [ROM 桩目录](../reference/rom-stubs.md)。

### 3. `-kernel` 启动跳过 bootloader

直接把 app 装入 RAM，**不经 flashboot**，所以 flash XIP 窗口默认是空的，C SDK 的分区表 / NV 读取会失败
（`[UPG] ...flash_start_addr fail`、`nv read sw fail`）。**解法：`NV=1`** 回填分区表 + NV（见
[运行固件](../how-to/run-firmware.md)）。但**逐芯片出厂标定键**（如 `xo_trim` 晶振温补）在生产时烧录，任何
构建产物的 NV 都没有，故 `xo_trim ... nv read sw fail` 一行**固有残留，非缺陷**。

### 4. 配置类外设是「影子」

寄存器可读回但无副作用——典型是 **RF / PHY / 晶体**等本质模拟量或物理硬件（`RF_WB_CTL`、`SHARE_MEM`、
`SPACC/PKE/KM` 等）。行为可内部计算的外设是真实的。为什么配置类只能是影子，见
[外设建模矩阵 §配置类为何是影子](../reference/peripheral-matrix.md#配置类为何是影子)。

### 5. 目标核 = 单核 RV32IMFC

单浮点（`F`）、压缩（`C`）、**无原子（`A` 关）**、**无 `D`**；WS63 是**单核**（无第二 hart）。
WS63 单核已经 fbb_ws63 C SDK 核实（`ch2_system.md` 明确「系统提供**一个**自研 RISC-V 处理器作为主控 CPU」；
Wi-Fi/BT 是链接进同一应用镜像的库，HMAC/DMAC 是 Wi-Fi 软件分层而非两颗物理核）。

## ROM 数据墙——仍然受限的东西

ROM 里不仅有**代码**（可在宿主 C 里仿真），还有**数据**（vtable 数据、子系统常量、出厂标定），数据没有 dump
就无法重建。以下因「ROM 数据墙」仍受限（均**非建模能解**）：

- **BT / WiFi / SLE 的射频（PHY/RF）** —— 物理边界，不仿；C SDK 含 BT/WiFi 的 app 会在子系统深层初始化崩于
  ROM 数据墙（vtable/NV/efuse/RF 标定无 dump），需在 `config.py` 注释 `BGLE/BTH/WIFI_TASK_EXIST` 裁剪这些任务。
- **逐芯片出厂标定 NV 键**（如 `xo_trim` 晶振温补）——生产时烧录，任何构建产物的 NV 都不含，固有残留一行
  `xo_trim_temp_comp::nv read sw fail`（非缺陷）。
- **看门狗中断模式回调**（"kick timeout!"）：WDT 超时→复位**已建模**，但超时→中断回调→再复位**未建模**
  （从虚拟时钟定时器回调改写 `env->pc` 注入固件回调不安全，定时器回调与 vCPU 取指边界不同步）。
- **真实密码学**（AES/SHA/RSA 的 SPACC v2 描述符协议）——未在启动路径，按需扩展。

详见 [ROM 桩目录 §C/§D](../reference/rom-stubs.md)。

## 冻结 / 非目标

均**非建模能解**或非本仿真器目标：

- **RF / PHY 无线电仿真**——不做；连接性若推进只到 MAC / SLIRP 边界（同 esp-qemu）。BS21/BS2X 的 BLE/SLE 连接性
  为何也是死胡同，见 [BS21 连接性可行性](bs21-connectivity-feasibility.md)。
- **双核 / 第二 hart**——WS63 单核（已核实），不做。
- **周期精确时序**——TCG 非微架构；`-icount` 仅 IPC=1 近似。真周期级请用 gem5 等。
- **snapshot / migration**——按需，默认不投入。

## FAQ

| 现象 | 原因 / 解法 |
|------|-------------|
| 固件一上来就大量 illegal instruction | 多为 xlinx ISA 或 ROM 调用；`DEBUG=1` 看 `qemu.log` 的 pc 落点，ROM 区（`0x109xxx`）是预期被拦截的 |
| C SDK app 反复打印 `flash_start_addr fail` / `nv read sw fail` | 用 **`NV=1`** 回填；残留的 `xo_trim` 一行是固有的 |
| C SDK BT/WiFi 任务崩溃 | 预期（ROM 数据墙）；裁剪 `BGLE/BTH/WIFI_TASK_EXIST` 任务 |
| 计时每次运行都不同 | 默认虚拟时间自由运行；要可复现用 **`ICOUNT=1`** |
| 看门狗样例"没看到 timeout 打印" | 中断模式回调未建模；健康喂狗→不复位是正确行为 |

## 相关

- 哪些外设建模到什么程度 → [验证覆盖范围](../reference/verification.md)、[外设建模矩阵](../reference/peripheral-matrix.md)
- ROM 桩清单 → [ROM 桩目录](../reference/rom-stubs.md)
- 设计取舍的背景 → [设计取舍](design-rationale.md)
