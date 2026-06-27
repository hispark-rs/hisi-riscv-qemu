# 机器模型参考

`-M ws63` 机器模型的组件清单、自定义 UART 寄存器布局、以及中断控制器的 CSR/IRQ 号。实现位于
`src/hw/riscv/ws63.c` + target/riscv 补丁。为什么这样设计见 [设计取舍](../explanation/design-rationale.md) 与
[中断控制器设计](../explanation/interrupt-controller.md)。

## 机器组件（`hw/riscv/ws63.c`）

| 组件 | 实现 |
|------|------|
| 机器类型 | `MACHINE_TYPE_NAME("ws63")` → `-M ws63` |
| CPU | 单 hart，命名 CPU `-cpu ws63`（默认型号）= **rv32imfc**（I/M/F/C + Zicsr/Zcf，关 A/D，无 MMU，禁 Zcb/Zcmp 让位 xlinx）|
| 复位 | `resetvec` = `-kernel` ELF 的 entry（缺省 `0x230300`）；无 OpenSBI/FDT |
| 内存 | BOOTROM/ROM/ITCM/DTCM/FLASH 作 RAM，SRAM 作 `-m` bank（见 [内存映射](memory-map.md)）|
| 固件载入 | `load_elf(-kernel, …, EM_RISCV, …)`，按 ELF 物理地址落段 |
| UART0/1/2 | 自定义 `ws63-uart` SysBusDevice @ `0x4401_0000/1000/2000`（见下） |
| TIMER | `ws63-timer`（3 个下数计数器 @ `0x4400_2000`，到点产生中断 26/27/28）|
| GPIO0/1/2 | `ws63-gpio`（输出 set/clr、输入、中断寄存器 @ `0x4402_8000/9000/A000`）|
| SYS_CTL0 | `ws63-sysctl0`（时钟状态：TCXO + PLL 已锁，使 `init_clocks()` 不空转）|
| 中断控制器 | `ws63-intc`：自定义 `LOCIxx` CSR 状态 + IRQ 路由（见下）|
| 其余外设 | `create_unimplemented_device` 吸收（三窗口），`-d unimp` 按地址可追踪 |

## 自定义 UART 设备

WS63 UART **不是** 16550（这是关键，QEMU 自带 `serial_mm` 不可用）。它是 HiSilicon 定制布局，
经 `WS63.svd` UART0 + `ws63-hal/src/uart.rs` + SDK `hal_uart_v151_regs_def.h` 核实：

| 偏移 | 寄存器 | 16550? |
|------|--------|--------|
| `0x00` | INTR_ID | ✗（16550 是 RBR/THR） |
| `0x04` | **DATA**（读=RX，写=TX） | ✗（16550 DATA 在 0x00） |
| `0x08` | UART_CTL | ✗ |
| `0x0C/0x10/0x14` | DIV_H / DIV_L / DIV_FRA（16 位整数 + 6 位小数分频） | ✗ |
| `0x34` | LINE_STATUS | 位定义不同于 16550 LSR |
| `0x44` | **FIFO_STATUS**：tx_full[0]/tx_empty[1]/rx_full[2]/rx_empty[3] | ✗ |

HAL 的 TX 路径（`uart.rs` `write_byte`）：轮询 `FIFO_STATUS.tx_fifo_full`（0x44 bit0）为 0 →
写 `DATA`（0x04）。模型据此：

- 读 `FIFO_STATUS` → TX 永远「空且不满」（瞬时排空），RX 视收到的字节而定。
- 写 `DATA` → `qemu_chr_fe_write_all` 输出到 chardev（`-serial mon:stdio`）。
- 读 `DATA` → 弹出收到的字节（最小 RX，支持回显）。
- 其余寄存器：接受写、读回 shadow / 0。

## 中断控制器（ws63-intc）

WS63 用 HiSilicon 自定义的「riscv31」核内 CLIC 式方案，不是 CLINT/PLIC。设备 IRQ 分两类。
投递机制的完整讨论见 [中断控制器设计](../explanation/interrupt-controller.md)；这里只列事实。

| IRQ 类 | 范围 | 使能机制 | 投递路径 |
|--------|------|----------|----------|
| **mie 类** | IRQ 26–31（TIMER_0/1/2、RTC、I2C0）| 标准 `mie` CSR 位 | `riscv_cpu_update_mip()` 拉高 `mip[n]` → 向量化 mtvec（`mtvec + 4*n`）|
| **自定义本地类** | IRQ ≥32（GPIO=33、UART=53…LSADC=72）| 核内自定义 CSR `LOCIEN`/`LOCIPD` | target/riscv 补丁核内投递，`mcause = irq(32–72)` → 向量化 mtvec |

自定义本地类相关 CSR：

| CSR | 地址 | 作用 |
|-----|------|------|
| `LOCIEN0-2` | `0xBE0` | 本地中断使能位 |
| `LOCIPD0-2` | `0xBE8` | 本地中断 pending 位 |
| `LOCIPCLR` | `0xBF0` | 清 pending |
| `LOCIPRI0-15` | `0xBC0` | 每 IRQ 4 位优先级（8 个/寄存器）|
| `PRITHD` | `0xBFE` | 优先级阈值；仅当优先级 **严格 > PRITHD** 才投递 |

复位默认：每 IRQ 优先级 1、阈值 0（退化为按号投递，兼容旧行为）；多候选取最高优先级，同级取最小 IRQ 号。

## 相关

- 各外设的建模状态与行为 → [外设建模矩阵](peripheral-matrix.md)
- 地址与外设基址 → [内存映射](memory-map.md)
- 中断投递机制的讨论 → [中断控制器设计](../explanation/interrupt-controller.md)
- 掩膜 ROM 拦截 → [ROM 桩目录](rom-stubs.md)
- 上游 RISC-V 机器/CPU 模型 → QEMU 官方 [RISC-V System emulation](https://www.qemu.org/docs/master/system/target-riscv.html)
