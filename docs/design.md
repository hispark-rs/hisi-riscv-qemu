# ws63-qemu 设计说明

## 目标

在 QEMU 上仿真 HiSilicon WS63（RISC-V）SoC，让 [`ws63-rs`](https://github.com/sanchuanhehe/ws63-rs)
裸机固件（blinky、UART 打印）无需真实硬件即可运行——为 ROADMAP 阶段 1「硬件在环 bring-up」
提供一个**软件在环**替代信号：证明内存布局、startup（PMP/FPU/cache/栈/数据重定位）、链接脚本
在一个 WS63 地址空间模型上能正确跑起来。

## 方法：fork 一个固定版本的 QEMU（仿 esp-qemu）

QEMU 没有稳定的「树外板卡插件」ABI，因此自定义 SoC 的标准做法是 fork 并加一个 in-tree 板卡文件
——Espressif 的 esp-qemu 即如此（`hw/riscv/esp32c3.c` + 自定义 CPU/外设）。本项目同样：

- 固定 **QEMU v9.2.4**（稳定线；`scripts/build.sh` 克隆该 tag）。
- 新增单文件 `hw/riscv/ws63.c`（机器模型 + 自定义 UART 设备），经 `meson.build` / `Kconfig` 两处
  极小注入接入构建（`scripts/build.sh` 自动完成，幂等）。
- 只构建 `riscv32-softmmu` 一个目标，控制构建时间（~10–20 分钟）。

> 为什么不用树外补丁：board 没有稳定插件 ABI；为什么不用 `-M virt`：固件按 WS63 地址链接，
> 首次访问 WS63 外设就会在 virt 上 fault。

## 机器模型（`hw/riscv/ws63.c`）

| 组件 | 实现 |
|------|------|
| 机器类型 | `MACHINE_TYPE_NAME("ws63")` → `-M ws63` |
| CPU | 单 hart，由可配置 `rv32` 核精确设为 **rv32imfc**（开 I/M/F/C，关 A/D/zawrs）|
| 复位 | `resetvec` = `-kernel` ELF 的 entry（缺省 `0x230300`）；无 OpenSBI/FDT |
| 内存 | BOOTROM/ROM/ITCM/DTCM/FLASH 作 RAM，SRAM 作 `-m` bank（见 [memory-map](memory-map.md)） |
| 固件载入 | `load_elf(-kernel, …, EM_RISCV, …)`，按 ELF 物理地址落段 |
| UART0/1/2 | 自定义 `ws63-uart` SysBusDevice @ `0x4401_0000/1000/2000` |
| TIMER | `ws63-timer`（3 个下数计数器 @ `0x4400_2000`，到点产生中断 26/27/28） |
| GPIO0/1/2 | `ws63-gpio`（输出 set/clr、输入、中断寄存器 @ `0x4402_8000/9000/A000`） |
| SYS_CTL0 | `ws63-sysctl0`（时钟状态：TCXO + PLL 已锁，使 `init_clocks()` 不空转） |
| 中断控制器 | `ws63-intc`：自定义 `LOCIxx` CSR 状态 + IRQ 路由（见下） |
| 其余外设 | `create_unimplemented_device` 吸收（三窗口），`-d unimp` 按地址可追踪 |

## 中断控制器（ws63-intc）

WS63 用 HiSilicon 自定义的「riscv31」核内 CLIC 式方案，不是 CLINT/PLIC。设备 IRQ 分两类：

- **IRQ 26–31（TIMER_0/1/2、RTC、I2C0）**：用**标准 `mie` 位**。固件经真实 `mie` CSR 使能；
  `ws63-intc` 收到外设 IRQ 线后用 `riscv_cpu_update_mip(env, 1<<n)` 拉高 `mip[n]`，QEMU 经
  **向量化 mtvec**（mode 1）派发到 `mtvec + 4*n`。**完整保真、已实测**（见 `timer_irq` 示例）。
- **IRQ ≥32（GPIO=33、UART=53…LSADC=72）**：用核内自定义 CSR `LOCIEN0-2`(0xBE0)/`LOCIPRI0-15`(0xBC0)/
  `LOCIPCLR`(0xBF0)，且 mcause 取值 32–72 **放不进 RV32 的 32 位 mip/mie**。`ws63-intc` 把这些 CSR 建模为
  **真实可读写状态**（使设置代码不陷阱），并记录 pending；但**核内向量化派发需要 patch `target/riscv`**，
  本模型暂不做（诚实标注，列为未来工作）。

> 即：定时器中断这条线是端到端验证过的；≥32 的自定义本地中断只建模了 CSR 状态，未做投递。

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

## 外设建模矩阵

| 外设 | 状态 | 说明 |
|------|------|------|
| CPU (rv32imfc) / 内存 / 复位 / ELF 载入 | ✅ 真实 | — |
| UART0/1/2 | ✅ 真实 | 自定义 HiSilicon 寄存器；TX 输出到 chardev，最小 RX |
| TIMER ×3 | ✅ 真实 | 下数计数器 + 中断（26/27/28），周期重载 |
| GPIO0/1/2 | ✅ 真实 | 输出 set/clr、输入读、中断寄存器；输出变化经 `qemu_log` 可见 |
| SYS_CTL0 | ✅ 真实(部分) | 仅时钟状态（TCXO/PLL 锁）；其余读 0 |
| 中断控制器 | 🟡 部分 | IRQ 26–31 完整投递；≥32 仅 CSR 状态（见上） |
| CLDO_CRG / TCXO | 🟡 吸收 | `init_clocks` 只写不读关键位，吸收即可 |
| I2C0/1, SPI0/1, PWM, I2S, LSADC, EFUSE, WDT, RTC, DMA, SDMA, SPACC/PKE/KM/TRNG, SFC | ⬜ 吸收 | catch-all 接受读写、读返回 0；按地址可追踪。尚无固件驱动它们；按本仓 device 模式可逐个增量建模 |

## 可观察的验证目标（均已实测 PASS，见 `scripts/smoke-test.sh`）

| 固件 | 触及 | 在 QEMU 中如何观察成功 |
|------|------|------------------------|
| `blinky` | GPIO0 输出翻转 | 0 非法指令陷阱；`ws63-gpio` 经 `-d` 打印 `out=...` 翻转 |
| `uart_hello` | UART0（跳过 `init_clocks`） | `-serial mon:stdio` 打印 `Hello from WS63 on QEMU!` |
| `timer_irq` | TIMER_0 → IRQ 26 → ISR | 串口打印 `timer irq #N` 递增 + `OK: timer interrupts delivered`（**中断端到端验证**） |

## 已知简化（未来工作）

1. **自定义本地中断（IRQ ≥32）的核内向量化投递未做**——需要 patch `target/riscv`（在 CPU 中实现
   `LOCIEN/LOCIPRI` 门控 + mcause 32–72 的投递）。当前只建模 CSR 状态。中断式 GPIO/UART/连接性驱动依赖此项。
2. **多数外设仅吸收**（见矩阵）。按 `ws63-timer`/`ws63-gpio` 的模式可逐个增量建模。
3. **CPU/时钟非周期精确**：定时器以名义 24 MHz 计时；无真实 PLL/时钟树。时序不保真。
4. 固定 v9.2.4；升级到 v10.x LTS 需注意 API 变化（如 `class_init` 的 `const void *data`、`sysemu/`→`system/` 头文件改名）。
