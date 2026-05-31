# WS63 内存映射与外设基址

本文件记录 `ws63-qemu` 机器模型所依据的 WS63 SoC 地址布局。**真值来源**：

- 内存区域（RAM/Flash/TCM）：`ws63-rs/ws63-rt/memory.x` + `layout.ld`
  ——这是固件实际链接所用的地址（Rust 运行时视角）。
- 外设基址：`ws63-rs/ws63-svd/WS63.svd`（各 `<peripheral>` 的 `baseAddress`）。

> ⚠️ 注意：fbb_ws63 C SDK 的 `platform_core.h` 使用了**另一套**（较旧/不同）地址布局
> （如 ITCM 在 0x80000）。`ws63-qemu` 以 `memory.x` 为准，因为它才是 ws63-rs 固件链接的地址。

## 内存区域（machine 建模）

| 区域 | 基址 | 大小 | QEMU 建模 | 说明 |
|------|------|------|-----------|------|
| BOOTROM | `0x0010_0000` | 36 KiB | RAM | 启动 ROM（QEMU 中跳过原厂 bootloader） |
| ROM | `0x0010_9000` | 268 KiB | RAM | 应用 ROM |
| ITCM | `0x0014_C000` | 16 KiB | RAM | 指令紧耦合内存 |
| DTCM | `0x0018_0000` | 16 KiB | RAM | 数据紧耦合内存 |
| FLASH | `0x0020_0000` | 8 MiB | RAM | XIP SPI NOR；`-kernel` ELF 载入于此 |
| PROGRAM | `0x0023_0300` | — | （FLASH 内） | 应用代码段起点 = **复位 PC / ELF entry** |
| SRAM | `0x00A0_0000` | 576 KiB | RAM (`-m` bank) | 主系统 RAM（data/bss/栈） |
| 栈顶 | `0x00A9_0000` | — | — | `ORIGIN(SRAM)+LENGTH(SRAM)` |

固件复位流程（`ws63-rt/asm/startup.S` → `src/startup.rs`）：关 PMP → 设 `mtvec` → 关中断 →
开 FPU → 设 `gp`/`sp` → 跳 `runtime_init`（开 cache、flash→RAM 数据重定位、清 BSS）→ `main`。

## 外设基址（来自 WS63.svd）

| 外设 | 基址 | `ws63-qemu` 状态 |
|------|------|------------------|
| SYS_CTL0 | `0x4000_0000` | 吸收（unimplemented）¹ |
| GLB_CTL_M | `0x4000_2000` | 吸收 |
| WDT | `0x4000_6000` | 吸收 |
| SYS_CTL1 | `0x4400_0000` | 吸收（自定义中断控制器，未建模） |
| CLDO_CRG | `0x4400_1100` | 吸收¹ |
| TCXO | `0x4400_04C0` | 吸收 |
| TIMER | `0x4400_2000` | 吸收 |
| EFUSE | `0x4400_8000` | 吸收 |
| LSADC | `0x4400_C000` | 吸收 |
| IO_CONFIG | `0x4400_D000` | 吸收 |
| **UART0** | `0x4401_0000` | **已建模**（自定义 HiSilicon UART 设备） |
| UART1/2 | `0x4401_1000` / `0x4401_2000` | 吸收 |
| I2C0/1 | `0x4401_8000` / `0x4401_8100` | 吸收 |
| SPI0/1 | `0x4402_0000` / `0x4402_1000` | 吸收 |
| PWM | `0x4402_4000` | 吸收 |
| I2S | `0x4402_5000` | 吸收 |
| GPIO0/1/2 | `0x4402_8000` / `0x4402_9000` / `0x4402_A000` | 吸收（blinky 的写入落于此） |
| SPACC/PKE/KM/TRNG | `0x4410_0000`..`0x4411_4000` | 吸收 |
| SFC_CFG | `0x4800_0000` | 吸收 |
| DMA | `0x4A00_0000` | 吸收 |
| SDMA | `0x520A_0000` | 吸收 |
| RTC | `0x5702_4000` | 吸收 |
| ULP_GPIO | `0x5703_0000` | 吸收 |

¹ **boot-critical**：`clock_init::init_clocks()` 会读 SYS_CTL0（TCXO 检测、PLL 锁定）与 CLDO_CRG
（时钟门控）。当前作吸收处理（读返回 0）。需要 UART 时建议固件**跳过** `init_clocks`
（QEMU 的 UART 忽略波特率，TX 照样输出）；或后续把这两个外设建模为返回安全状态。

"吸收"= `create_unimplemented_device`：接受所有读写、读返回 0、不会让 VM 崩溃，
可用 `-d unimp` 追踪。三个吸收窗口：`0x4000_0000`(256 MiB)、`0x5200_0000`、`0x5700_0000`。

## CPU

- 真实芯片：**RV32IMFC_Zicsr**（硬件单精度浮点 `ilp32f`，**无原子扩展 A**），240 MHz，单 hart。
- QEMU 默认用 `sifive-e34` 核（rv32imafc）——是 rv32imfc 的超集；固件由工具链保证不发原子指令，
  故多出的 `A` 无副作用。复位 PC = ELF entry（`0x0023_0300`），无 OpenSBI / 无 FDT（裸机）。
