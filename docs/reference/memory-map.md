# WS63 内存映射与外设基址

本文件记录 `ws63-qemu` 机器模型所依据的 WS63 SoC 地址布局。**真值来源**：

- 内存区域（RAM/Flash/TCM）：`ws63-rs/ws63-rt/memory.x`，它是 fbb_ws63 C SDK 板级内存配置
  `src/drivers/boards/ws63/evb/memory_config/include/memory_config_common.h` 的忠实转写。
- 外设基址：`ws63-rs/ws63-pac/ws63-svd/WS63.svd`（各 `<peripheral>` 的 `baseAddress`；svd 现为 ws63-pac 的嵌套子模块）。

> ✅ **与 C SDK 一致**：上表地址逐项匹配 `memory_config_common.h`——
> `BOOTROM_START=0x100000`、`ROM_START=0x109000`、`APP_ITCM_ORIGIN=0x14C000`、
> `APP_DTCM_ORIGIN=0x180000`、`APP_SRAM_ORIGIN=0xA00000`、`FLASH_START=0x200000`、
> `APP_PROGRAM_ORIGIN=0x230000+0x300=0x230300`。memory.x 与 C SDK **不冲突**。
>
> 易混淆点：`platform_core.h` 里的 `MPU_ITCM_ADDR_BASE=0x80000`、`MPU_L2RAM_ADDR0_BASE=0x100000`
> 是 **MPU 保护区窗口**（供内存保护单元用的粗粒度地址范围），**不是**代码/数据的实际链接地址，
> 是另一层概念。应用固件的真实放置以 `memory_config_common.h` / `memory.x` 为准。

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

状态:**已建模** = 专有 sysbus 设备(寄存器语义 + 必要时中断/搬运);**已建模(部分)** = 关键位真实、
其余影子;**吸收** = `create_unimplemented_device` 兜底(读 0、不崩)。行为细节见 [外设建模矩阵](peripheral-matrix.md)。

| 外设 | 基址 | `ws63-qemu` 状态 |
|------|------|------------------|
| SYS_CTL0 | `0x4000_0000` | **已建模**（时钟状态 TCXO/PLL 锁 + 系统复位记录） |
| GLB_CTL_M | `0x4000_2000` | **已建模**（在 SYS_CTL0 窗口内:芯片复位触发） |
| WDT | `0x4000_6000` | **已建模**（倒计时→超时复位 SoC） |
| TCXO / SYS_CTL1 窗口 | `0x4400_0000`（计数器 `0x4400_04C0`） | **已建模(部分)**（64 位单调计数 + count-valid;覆盖 SYS_CTL1 区） |
| CLDO_CRG | `0x4400_1100` | **已建模(部分)**:时钟门控生效（清/置 CKEN_CTL0 冻结/恢复定时器）¹ |
| TIMER | `0x4400_2000` | **已建模**（×3 下数计数器 + 中断 26/27/28） |
| RF_WB_CTL | `0x4400_4000` | 吸收（配置影子;RF/PHY 无线电**不仿真**） |
| EFUSE | `0x4400_8000` | **已建模**（OTP:写=按位或、STS boot-done、数据窗读回） |
| LSADC | `0x4400_C000` | **已建模**（触发转换 → 弹 14-bit 采样 + IRQ 72） |
| IO_CONFIG (pinmux) | `0x4400_D000` | **已建模**（`ws63-pinmux` 引脚复用织构） |
| TSENSOR | `0x4400_E000` | **已建模**（start→rdy + 10-bit 温度码） |
| **UART0** | `0x4401_0000` | **已建模**（自定义 HiSilicon UART 设备） |
| UART1/2 | `0x4401_1000` / `0x4401_2000` | **已建模**（同 UART0） |
| I2C0/1 | `0x4401_8000` / `0x4401_8100` | **已建模**（回环 FIFO:TXR→RXR、SR 完成位、IRQ 31/32） |
| SPI0/1 | `0x4402_0000` / `0x4402_1000` | **已建模**（回环 FIFO:DR 写→顺序读回、WSR、IRQ 43/52） |
| PWM | `0x4402_4000` | **已建模(部分)**（影子 + START 自清 + PERIODLOAD_FLAG） |
| I2S | `0x4402_5000` | **已建模**（LEFT/RIGHT TX→RX 回环） |
| GPIO0/1/2 | `0x4402_8000` / `0x4402_9000` / `0x4402_A000` | **已建模**（输出 set/clr、输入、边沿/电平中断、真实信号网） |
| SPACC / PKE / KM | `0x4410_0000` / `0x4411_0000` / `0x4411_2000` | 吸收（寄存器影子） |
| TRNG | `0x4411_4000` | **已建模**（FIFO_READY + 伪随机 xorshift） |
| SFC | `0x4800_0000` | **已建模(部分)**（SPI 命令 RDID/RDSR + flash XIP 窗口） |
| DMA | `0x4A00_0000` | **已建模**（通道使能即真正搬运内存 + 完成位 + IRQ 59） |
| SDMA | `0x520A_0000` | **已建模**（同 DMA 引擎;逻辑通道 8–11） |
| RTC | `0x5702_4000` | **已建模**（周期触发 IRQ 29 + CURRENT_VALUE 计数） |
| ULP_GPIO | `0x5703_0000` | 吸收（寄存器影子） |

¹ **boot-critical**：`clock_init::init_clocks()` 读 SYS_CTL0（TCXO 检测、PLL 锁定）+ 写 CLDO_CRG
（时钟门控）。SYS_CTL0 已建模为返回「TCXO 检出 + PLL 已锁」,故 `init_clocks()` 不空转;CLDO_CRG
的时钟门控位真实生效(清 CKEN_CTL0 bit21 冻结定时器、置位恢复),其余位影子。

> **建模 vs 吸收的实现**:已建模外设是注册的 sysbus 设备,映射**覆盖**在三个 `create_unimplemented_device`
> 兜底窗口(`0x4000_0000` 256 MiB、`0x5200_0000`、`0x5700_0000`)之上;只有未建模子区落到兜底
> （读 0、不崩,可用 `-d unimp` 追踪）。**全部 35 个 SVD 外设均已覆盖**(见 [外设建模矩阵](peripheral-matrix.md))。

## CPU

- 真实芯片：**RV32IMFC_Zicsr**（硬件单精度浮点 `ilp32f`，**无原子扩展 A**），240 MHz，单 hart。
- QEMU 用命名 CPU `-cpu ws63`（默认型号），**= I/M/F/C + Zicsr/Zcf，关闭 A、D，无 MMU**——与 WS63 ISA
  完全一致（非超集；Zcb/Zcmp 禁用以让位 xlinx 自定义压缩编码）。复位 PC = ELF entry（`0x0023_0300`），
  无 OpenSBI / 无 FDT（裸机）。
  注：`zawrs` 在基础核默认开启且依赖 A，故一并关闭以保持 A 关闭。

## 相关

- 机器组件与自定义 UART 寄存器 → [机器模型](machine-model.md)
- 各外设的行为细节 → [外设建模矩阵](peripheral-matrix.md)
- 内存/启动为何这样设计 → [设计取舍](../explanation/design-rationale.md)
