# 验证覆盖范围

ws63-qemu「覆盖」分三层：**持续 CI 验证**（每次提交都跑）、**外设建模覆盖**（35/35 建模）、**明确的边界**
（已知不覆盖）。本文列出各层的具体清单。怎么自己跑这些测试见 [运行测试](../how-to/run-tests.md)。

## 持续验证（CI）

`ci.yml` 在**每次 push / PR** 上跑（Ubuntu），门禁全绿才算通过：

1. 构建 `qemu-system-riscv32`（带 WS63 机器）；
2. **机器注册** sanity（`-M help` 含 ws63）；
3. **ws63-rs 冒烟**（`smoke-test.sh`，见下）；
4. **C SDK 外设样例**（`csdk-test.sh`，见下，**5/5**）；
5. **寄存器级 qtest**（`qtest.sh`，免启动驱动 GPIO/UART/timer/INTC/DMA，**4/4**）。

`release.yml`（打 `v*` tag 时）额外做全新构建 + 冒烟，然后发布二进制。
`qtest-matrix.yml` 在每个有 `patches/<tag>/` 的 QEMU 版本上跑 qtest（v9.2.4 + v10.0.0 + v10.2.3 + v11.0.1 全绿）。

## ws63-rs（Rust）冒烟

`scripts/smoke-test.sh`（真值见脚本）；每条都是端到端、断言串口/MMIO 标志：

| 固件 | 验证什么 | 成功判据 |
|------|----------|----------|
| `blinky` | GPIO0 输出翻转 + xlinx/启动正确 | 0 非法指令陷阱 + 观察到 GPIO 翻转（pin0 拉高）|
| `uart_hello` | 自定义 UART0 TX | 串口打印 `Hello from WS63 on QEMU!` |
| `timer_irq` | TIMER_0→**IRQ 26**→ISR（mie 类中断）| 串口 `timer irq #N` 递增 + `OK: timer interrupts delivered` |
| `gpio_irq` | GPIO0→**IRQ 33**→ISR（**≥32 自定义本地中断**）| 串口 `gpio irq #N` + `OK: custom local IRQ (>=32) delivered` |
| `reset_demo` | `software_reset()` + `reset_reason()` 往返 | 重启 ≥2 次 + `OK: software reset observed`（reset_reason=Software）|
| `dma_loopback` | mem↔SPI0 外设 DMA + SDMA 通道 | `DMA LOOPBACK TEST: PASS` |
| `wifi_blob_link` | 链接 `libwifi_rom_data.a` + 重定位 | `BLOB LINK SPIKE: PASS` |
| `rf_port_demo` | ws63-rf-rs porting 层 + blob 经其链接 | `RF PORT DEMO: PASS` |
| `sched_selftest` | ws63-rf-rs 协作调度器（上下文切换 + 信号量）| `SCHED SELFTEST: PASS` |
| `semihost_selftest` | semihosting 退出码（M/F/Zicsr 自检）| **QEMU 退出码 0**（免解析 UART;见 [运行选项](run-options.md) `SEMIHOST`）|

## C SDK 外设样例

`scripts/csdk-test.sh` 启动 `tests/csdk/` 里**预编译的 fbb_ws63 C SDK 厂商固件**并断言各自的 UART 成功标志——
用**真实厂商固件**交叉验证外设模型。纯净 + 免 SDK/工具链（用已提交的 fixture）。**当前 5/5 绿**：

| 样例 | 验证的外设路径 | 成功标志 |
|------|----------------|----------|
| `tcxo.elf` | TCXO ms/us 计数器 | `tcxo get ms work normall` |
| `systick.elf` | SysTick 计数器 | `systick get ms work normall` |
| `adc.elf` | LSADC 标定 + RX-FIFO 转换读 | `voltage: N mv` |
| `dma.elf` | DMA v151 内存搬运 + 完成 | `dma memory copy test succ` |
| (NV overlay) | 分区表解析 + NV 读 | 启动到调度器且无 `upg ...flash_start_addr fail` |

**已记录但未断言的样例**（`tests/csdk/manifest.txt`）：

- **`timer`** —— **非外设模型问题**。硬件定时器模型正确（channel 0 / IRQ 26 即 LiteOS systick 正常触发），
  但 LiteOS **软件定时器任务层**没走到 `uapi_timer_start`，属上层任务问题。
- **`watchdog`** —— 看门狗 API **功能可用**（喂狗→重装、超时→复位;健康样例干净跑到调度器），但样例唯一的成功
  标志是**中断模式**的 `"watchdog kick timeout!"` 回调，而该回调**未建模**（见 [已知边界](../explanation/limitations.md)），
  故无 UART marker 可断言。

> 重新生成 fixture：`scripts/build-csdk-samples.sh`（从 fbb_ws63 checkout 选一个 `CONFIG_SAMPLE_SUPPORT_*`、
> 干净构建、strip 到 ~400 KB）。

## 外设建模覆盖

`WS63.svd` 的**全部 35 个外设均已建模**（无裸 catch-all 黑洞）。分两档，完整矩阵见 [外设建模矩阵](peripheral-matrix.md)：

- **行为完整（真实数据/计时/中断/回环）**：CPU+内存、xlinx ISA、UART0/1/2（TX+RX）、TIMER×3、GPIO+pinmux、
  中断控制器（26–31 + ≥32，含 LOCIPRI/PRITHD）、DMA/SDMA、RTC、WDT、I2C0/1、SPI0/1、I2S、LSADC、TSENSOR、
  EFUSE、TRNG、TCXO、SFC、CLDO_CRG 时钟门控。
- **配置影子（可读回、无副作用）**：RF_WB_CTL、SHARE_MEM、SPACC/PKE/KM、部分 SYS_CTL1/IO_CONFIG 位等。

## 明确不在覆盖范围内

| 项 | 状态 | 原因 |
|----|------|------|
| Wi-Fi / BT / SLE 射频（PHY/RF）| ❌ 不仿 | 物理边界 |
| C SDK 含 BT/WiFi app 的深层初始化 | ❌ 崩于 ROM 数据墙 | vtable/NV/efuse/RF 标定无 dump，需裁剪任务 |
| 逐芯片出厂标定 NV（`xo_trim` 等）| ❌ 固有缺失 | 生产时烧录，任何构建产物都不含 |
| 真实 AES/SHA/RSA（SPACC v2）| ❌ 影子 | 描述符协议复杂且未被触发 |
| 看门狗中断模式超时回调 | ❌ 未建模 | 需 vCPU 同步的 PC 注入 |
| 周期精确时序 | ❌ 近似 | TCG 非微架构;`ICOUNT` 仅 IPC=1 近似 |
| 双核 / 第二 hart | ❌ 不适用 | WS63 单核（已核实）|
| snapshot / migration | ❌ 未投入 | 按需 |

这些边界的展开讨论见 [已知边界与非目标](../explanation/limitations.md)。

## 相关

- 怎么跑这些测试 → [运行测试](../how-to/run-tests.md)
- 每个外设的建模细节 → [外设建模矩阵](peripheral-matrix.md)
- 边界背后的原因 → [已知边界与非目标](../explanation/limitations.md)
