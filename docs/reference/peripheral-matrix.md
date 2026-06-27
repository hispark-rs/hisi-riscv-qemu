# 外设建模矩阵

`WS63.svd` 的**全部 35 个外设均已建模**（无裸 catch-all 黑洞）。本表逐个列出建模状态。地址见
[内存映射](memory-map.md)，机器组件见 [机器模型](machine-model.md)，配置类为何是影子见文末。

| 外设 | 状态 | 说明 |
|------|------|------|
| CPU (rv32imfc) / 内存 / 复位 / ELF 载入 | ✅ 真实 | 命名 CPU `-cpu ws63`（默认型号）:I/M/F/C + Zicsr/Zcf,关 A/D、无 MMU,禁用 Zcb/Zcmp(让位 xlinx 压缩编码);见 target/riscv 补丁 |
| **xlinx 自定义 ISA** | ✅ 真实 | HiSilicon riscv31 私有指令（l.li/\*shf/b\*i/muliadd/jal16/ldmia/push-pop/压缩 lbu-sb…），**厂商 gcc 固件必需**；见 [xlinx ISA](xlinx-isa.md) |
| UART0/1/2 | ✅ 真实 | 自定义 HiSilicon 寄存器；TX→chardev，RX←chardev（中断使能时触发 IRQ 53/54/55）|
| TIMER ×3 | ✅ 真实 | 下数计数器 + 中断（26/27/28），周期重载 |
| GPIO0/1/2 | ✅ **真实(行为完整)** | 输出 set/clr、输入读、边沿/电平中断；引脚为**真实信号网**：bank 内输出→输入回环 + **跨 bank 板级连线**（GPIO0 输出脚驱动 GPIO1 输入脚，可观测+中断，裸机验证）+ 可由 monitor/外部设备驱动 |
| SYS_CTL0 | ✅ 真实(部分) | 仅时钟状态（TCXO/PLL 锁）；其余读 0 |
| **TCXO 时钟/计数器** | ✅ 真实(部分) | `0x440004C0`：bit4 count-valid + 64 位单调计数（+0x04/+0x08），供 bootloader us 级延时 |
| **PPB（核内私有外设总线）** | 🟡 RAM 吸收 | `0xE0000000` FlashPatch 单元 + Cortex-M 式 SCS（`0xE000E000`）；加载已打补丁镜像故补丁单元无意义 |
| 中断控制器 | ✅ 真实 | IRQ 26–31（mie 类）+ ≥32（自定义 LOCIxx，target/riscv 补丁）均完整投递；`LOCIPRI` 优先级 + `PRITHD` 阈值已强制（严格 `>`、最高优先级优先、同级取小号）|
| **SFC（Flash 控制器）** | ✅ 真实(部分) | SPI 命令接口（RDID→W25Q16、RDSR→ready、命令完成）；flash XIP 窗口（0x200000）为 RAM 背靠，默认空——可用 `run.sh NV=1` 回填分区表+NV（见 [运行固件](../how-to/run-firmware.md)）|
| **I2C0/1**（0x44018000/0x44018100）| ✅ **真实(行为完整)** | 真实回环 FIFO：TXR→RXR 多字节顺序回读、SR 完成位、COM 命令位自动清、IRQ 31/32 |
| **SPI0/1**（0x44020000/0x44021000）| ✅ **真实(行为完整)** | 真实回环 FIFO：DR 写入→顺序读回、WSR 反映 FIFO（rxfne/txfe）、RLR 深度、IRQ 43/52 |
| **PWM**（0x44024000）| ✅ 真实(部分) | 寄存器影子 + PERIODLOAD_FLAG=1 + START 自清 |
| **I2S**（0x44025000）| ✅ 真实(行为完整) | LEFT/RIGHT TX→RX 回环 |
| **LSADC**（0x4400C000）| ✅ **真实(行为完整)** | CTRL_8 触发转换 → rne=1/bsy=0、CTRL_9 弹出 14-bit 采样 + 完成 IRQ 72（读清）|
| **EFUSE**（0x44008000）| ✅ **真实(行为完整)** | 真实 OTP：STS boot-done + 数据窗读回，**写=按位或**（一次性熔丝只能置位，不可清零，裸机验证）；标定内容无 dump 故为空白熔丝 |
| **TSENSOR**（0x4400E000）| ✅ **真实(行为完整)** | start→sts rdy=1 + 10-bit 温度码（合成 ~25°C，按 HAL 转换公式）|
| **WDT**（0x40006000）| ✅ **真实(行为完整)** | QEMU 定时器倒计时；超时未喂狗则真复位 SoC（裸机测试验证）|
| **RTC**（0x57024000）| ✅ **真实(行为完整)** | QEMU 定时器周期触发 **IRQ 29** + INT_STATUS/EOI；CURRENT_VALUE 计数 |
| **DMA/SDMA**（0x4A000000/0x520A0000）| ✅ **真实(行为完整)** | 通道使能即**真正搬运内存**（src→dst，按宽度/地址自增），置传输完成位，按 tc_int_en 触发 **IRQ 59**；INT_CLR 清除（裸机测试验证）|
| **TRNG**（0x44114000）| ✅ 真实 | FIFO_READY=ready、FIFO_DATA 伪随机（xorshift）|
| **合成 Wi-Fi/以太 MAC**（`ws63-netmac` @ 0x44210000）| ✅ **真实(行为完整,合成)** | 软件在环连接性底座（路线图阶段 5）：**不仿 RF/PHY**，在 ws63-rf-rs netif 缝合点暴露最小以太帧 MAC——TX_BUF+TX_GO→`qemu_send_packet`（接 `-nic user` SLIRP NAT），主机帧→`.receive`→RX_BUF + **IRQ 45**（WLMAC_INT）；qtest 整帧收发回环验证。非厂商 WLMAC 寄存器级复刻 |
| **SPACC / PKE / KM**（密码学）| 🟡 影子 | 寄存器影子（**未在启动路径**：mbedtls 用 ROM 表软件 AES）。真实 AES/SHA/RSA 可经 QEMU crypto 库实现，但 SPACC v2 多通道描述符协议复杂且无固件触发，列为按需扩展 |
| **CLDO_CRG**（时钟与复位生成）| ✅ 真实(部分) | 时钟门控生效：清 CKEN_CTL0 bit21 冻结定时器、置位恢复（已实测）；CLK_SEL 源路由建模为状态；其余位影子 |
| **IO_CONFIG / SYS_CTL1 / PWM / RF_WB_CTL / SHARE_MEM / FAMA_REMAP / ULP_GPIO**（影子）| 🟡 影子 | 见下「配置类为何是影子」 |

> **覆盖度**：`WS63.svd` 的全部 **35 个外设**现均有模型（无裸 catch-all 黑洞）。
> 「行为完整」= 真实数据搬运/计时/中断/转换（DMA/RTC/WDT/Timer/I2C/SPI/I2S/LSADC/GPIO/UART/**TSENSOR/EFUSE**）。

## 配置类为何是影子

配置寄存器本身没有"行为"，其行为是对*别处*的*作用*。作用可内部计算的已做成真实
（TSENSOR 出温度、EFUSE 走 OTP、LSADC 出采样）；作用是**物理/外部**的则在仿真器里无可观测行为：

- **引脚行为本身可仿真**：QEMU 用信号网（`qemu_irq`）建模引脚——GPIO 引脚是真实信号网
  （bank 内回环 + 跨 bank 板级连线 + 可外部驱动）。真正"不可观测"的只是**悬空引脚**（输出无连接对象）。
- **IO_CONFIG（引脚复用）= `ws63-pinmux` 设备（已做路由）**：`GPIO_xx_SEL[2:0]` 选功能（0=GPIO，
  1-7=UART/SPI/I2C/PWM…）。GPIO0→GPIO1 的板级引脚网**经 pinmux 路由**：引脚复用为 GPIO 时信号导通、
  复用为其他功能时被门控（引脚改由该外设承载）。裸机验证：复用 GPIO→读到 `1`，复用走→`0`，复用回→`1`。
  非 GPIO 功能（UART TX/SPI CLK）的数据本身由各外设的 TX/RX/回环覆盖；pinmux 负责的是"哪根物理引脚承载它"。
- **RF_WB_CTL / WiFi / BT**：射频 PHY，物理边界，不仿。
- **CLDO_CRG 时钟门控（已生效）**：定时器门（CKEN_CTL0 bit21）清零会冻结定时器、置位恢复（默认开，
  故不显式开门的固件不受影响）；CLK_SEL 源路由建模为状态。其*复位位*理论可复位目标外设，但位→外设映射
  复杂且无固件触发，暂留影子。
- **SHARE_MEM_CTL**：核间共享内存控制，单核下无意义。
- **SPACC/PKE/KM**：见加密行，真实 AES/SHA/RSA 需复杂且未被触发的描述符协议，按需扩展。

## 相关

- 地址 → [内存映射](memory-map.md)
- 机器组件与 UART 寄存器 → [机器模型](machine-model.md)
- 哪些明确不覆盖、为什么 → [已知边界与非目标](../explanation/limitations.md)
- 行为完整外设是怎么验证的 → [验证覆盖范围](verification.md)
