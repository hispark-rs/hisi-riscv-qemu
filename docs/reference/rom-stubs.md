# WS63 掩膜 ROM 桩与拦截目录

本文记录 ws63-qemu 中**所有与掩膜 ROM(mask ROM)相关的桩(stub)/拦截(interception)**的清单：机制、已仿真的 ROM
函数、相邻设备桩。真值来源：`patches/<QEMU_TAG>/0001-target-riscv-*.patch`（`target/riscv/cpu_helper.c`）与
`src/hw/riscv/ws63.c`。

- 为什么需要这些桩、ROM 数据墙为何仍受限 → [已知边界与非目标](../explanation/limitations.md)
- 怎么新增/确认一个 ROM 桩 → [扩展掩膜 ROM 桩](../how-to/extend-rom-stubs.md)

## 为什么需要这些桩（简述）

WS63 的 RISC-V 应用核有一块**片上掩膜 ROM**（地址区间 `0x109000 .. 0x14C000`），固化了启动期与基础库代码/数据
（`mem*_s` / `*printf_s`、systick/tcxo 计时、各外设 HAL vtable、整套看门狗 API、BT/WiFi 标定数据）。**HiSpark SDK
不发布这块 ROM 的二进制**，固件 `call` 到这些地址时 QEMU 那块区域是空的（取指得 `0` → 非法指令异常）。

我们用两类手段：**(A) 掩膜 ROM 调用拦截**（把 ROM 函数在宿主 C 里仿真）、**(B) ROM/启动相邻的设备桩**。

## A. 掩膜 ROM 调用拦截（`ws63_rom_call`）

**机制**：`riscv_cpu_do_interrupt()` 里加了一个钩子——当一次**非法指令异常**的 `pc` 落在 ROM 区间
`[0x109000, 0x14C000)` 时，**不进入正常陷阱**，而是调用 `ws63_rom_call(env)`，在宿主 C 里仿真该 ROM 函数，
然后：`a0(gpr[10]) = 返回值;pc = ra(gpr[1])`，即**直接返回到调用者**。靠 `pc` 分派到具体函数；**未识别的
ROM 地址走 `default` 分支，返回 0(成功)**，让固件能调用未建模的 ROM 函数而不崩（优雅降级）。

已仿真的 ROM 函数：

| ROM 地址 | 函数 | 仿真内容 |
|----------|------|----------|
| `0x10aeb4` | `memcpy_s(dst,dmax,src,n)` | 安全拷贝;`n>dmax` 返回 -1,否则逐字节拷贝、返回 0 |
| `0x10b790` | `memmove_s` | 带重叠安全(临时缓冲);`n>dmax` 返回 -1 |
| `0x10b7f6` | `memset_s(dst,dmax,c,n)` | 安全填充;`n>dmax` 返回 -1 |
| `0x10cc3c` | `sprintf_s` | 经 `ws63_vformat` 解析 guest 变参格式化到 `dst`,返回长度 |
| `0x10cc0a` | `snprintf_s` | 同上,长度截到 `min(count,dmax)` |
| `0x10d1d0` | `vsnprintf_s` | 同上,但变参取自 guest 的 `va_list`(`use_va=true`) |
| `0x10ac44/0x10acba/0x10d34a` | `uapi_systick_get_count/_us` / `uapi_tcxo_get_us` | 共用:静态 us 计数每次 +1000,`a0`=低 32 位、`a1`=高 32 位(**保证单调递增**,bootloader 的 us 级延时不会死等) |
| `0x10ac94/0x10d32c` | `uapi_systick_get_ms` / `uapi_tcxo_get_ms` | 共用:静态 ms 计数每次 +1 |
| `0x10aa9a` | `hal_timer_v150_get_funcs` | 返回合成 vtable 指针(`ws63_rom_vtable(0)`):一块 256B 的 scratch(在 PPB 的 `~0xE000F000`),预填 `WS63_ROM_BASE` 蹦床地址(被调到时再走 ROM 拦截→返回 0) |
| `0x109ab8` | `hal_sfc_v150_funcs_get` | 同上(`ws63_rom_vtable(1)`),供 flashboot 的 SFC 初始化拿到非空 vtable |
| `0x109f7e` | `uapi_watchdog_init(timeout_s)` | 记录超时秒数(0→默认 2);见 §C |
| `0x109fb6` | `uapi_watchdog_enable(mode)` | `ws63_wdt_rearm()` 起一个虚拟 one-shot 定时器 |
| `0x10a0a4` | `uapi_watchdog_kick` | 重新装填定时器(喂狗) |
| `0x109fda/0x10a00e` | `uapi_watchdog_disable/_deinit` | `timer_del` 取消定时器 |
| *(其它)* | `default` | 返回 0(成功),不崩 |

**结果**：`ws63-liteos-app` 能启动 LiteOS 到 `cpu 0 entering scheduler`、格式化日志正常打印
（`*printf_s` 走宿主 libc）、SFC/timer 的 vtable 获取不返回空指针。

## B. ROM / 启动相邻的设备桩（`src/hw/riscv/ws63.c`）

这些不是"ROM 函数仿真"，而是为了让 **bootloader/app 的 bring-up 寄存器序列**走通而建模的设备：

| 设备/区域 | 地址 | 桩内容 |
|-----------|------|--------|
| `ws63-tcxo` | `0x440004C0` | 24 MHz **单调计数器**(count-valid 位常 1,计数寄存器每次读保证步进)。QEMU 虚拟时钟在紧凑 TCG MMIO 轮询里会"冻结",故必须保证每读必增,否则 flashboot 的 us 延时死等 |
| **PPB**(核内私有外设总线) | `0xE0000000`(64KB) | RAM 背靠:FlashPatch 单元 + Cortex-M 式 SCS(`0xE000E000`);ROM vtable getter 返回的 scratch vtable 落在 `~0xE000F000` |
| **SYS_CTL0** | `0x40000000`(16KB) | 时钟状态:`HW_CTL` 读 0(TCXO 24MHz)、`REG_EXCEP_RO_RG`@`0x319C` 的 bit12=1(PLL 已锁),使 `init_clocks()` 走通 |
| `ws63-sfc` | `0x48000000` | SPI flash 命令接口:`RDID`→W25Q16(`0x001560EF`)、`RDSR`→ready/未保护、start 位自动清 |
| **Flash XIP 窗口** | `0x200000`(8MB) | RAM 背靠,**默认空**;`run.sh NV=1` 用 `-device loader` 回填分区表(`partition_params.bin`@`0x200000`,表在 `0x200380`,magic `0x4b87a54b`)+ NV(见 [csdk 测试 fixtures](../../tests/csdk/flash/manifest.txt)) |
| 低位 MMIO 兜底吸收 | `0x40000000`(256MB) | `create_unimplemented_device`,吸收未建模外设;SYS_CTL0/SFC 等以更高优先级映射在其上 |

**启动参数交接**(`ws63_cpu_reset`)：复位时 `a0(gpr[10]) = WS63_SRAM_BASE`(指向一个可读、清零的 SRAM 字)。
真机上每个启动级(mask ROM → loaderboot → flashboot → app)以 `a0 = 启动参数块指针` 进入;独立 `-kernel` 启动
没有上一级,`a0` 会是 0,而 bootloader 在设置 `mtvec` 之前就解引用 `a0`(`lw t3,0(a0)`)→ 取数异常→双重异常→
`pc=0`。指 `a0` 到清零 SRAM 让"启动原因"读到 0(正常启动)。

## C. 看门狗 ROM API 仿真（展开）

**整套**看门狗栈都在掩膜 ROM 里（`uapi_watchdog_*`、`hal_watchdog_v151_*`、`watchdog_port_*`），所以最初每个调用
都被 `default` 桩成 no-op。**注意**：仅"强行设 `g_watchdog_regs`"是不够的——调用方（`uapi_watchdog_init` 等）本身
就是 ROM 桩，根本走不到 HAL。

因此在 `ws63_rom_call` 里**仿真整套 API**：`init` 记超时、`enable` 用 `QEMU_CLOCK_VIRTUAL` 起一个 one-shot 定时器、
`kick` 重新装填、`disable/deinit` 取消;定时器到期(= 固件停止喂狗)时 `ws63_wdt_fire()` 调
`qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET)`——这正是看门狗的本职。**已验证**：C SDK watchdog 样例
持续喂狗 → 永不复位、干净跑到调度器，挂死才会触发复位请求。

**未建模**：中断模式的**超时回调**（样例里的 `"watchdog kick timeout!"`）。从虚拟时钟定时器回调里改写
`env->pc` 去注入固件回调并不安全（定时器回调与 vCPU 取指边界不同步），会被静默丢弃；故仅建模"超时→复位"，
不建模"超时→中断回调→(未喂狗)再复位"。见 [已知边界与非目标](../explanation/limitations.md)。

## 相关

- 仍受限的东西（ROM 数据墙：BT/WiFi、`xo_trim`、密码学、看门狗中断回调）→ [已知边界与非目标](../explanation/limitations.md)
- 怎么确认/新增一个桩 → [扩展掩膜 ROM 桩](../how-to/extend-rom-stubs.md)
- xlinx ISA 与 ROM 的关系 → [xlinx ISA](xlinx-isa.md)
