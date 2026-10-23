# ws63-qemu

在 **QEMU** 上仿真 HiSilicon **WS63**（RISC-V RV32IMFC，Wi-Fi 6 + BLE + SLE/星闪 SoC），
用于无硬件运行仓库内预构建厂商固件，也可运行 [`ws63-rs`](https://github.com/hispark-rs/hisi-riscv-rs)
裸机固件做开发验证。

仿照 [esp-qemu](https://github.com/espressif/qemu) 的做法：fork 一个固定版本的 QEMU，加入一个
in-tree 板卡文件 `hw/riscv/ws63.c`（WS63 机器模型 + 自定义 UART），只构建 `riscv32-softmmu`。

> 状态：单核 SoC 模型。CPU + xlinx 自定义 ISA、内存、中断（IRQ 26–31 + 自定义本地 ≥32，含 LOCIPRI/PRITHD
> 优先级阈值）、**全部 35 个 SVD 外设**（DMA/RTC/WDT/I2C/SPI/I2S/LSADC/UART/TSENSOR/EFUSE/TRNG/SFC +
> GPIO 引脚网/pinmux + CLDO_CRG 时钟门控）均已建模；跑通 ws63-rs（blinky/uart_hello/timer_irq/gpio_irq）与
> fbb_ws63 C SDK（flashboot + ws63-liteos-app 启动到调度器）。可选 `-icount` 确定性指令计时。
> 开发/测试基建齐备:GDB(`-s -S`)、qtest 寄存器级回归、semihosting 退出码、GPIO/DMA trace 事件、命名 `-cpu ws63`。
> **在线文档站（mdBook）：<https://hispark-rs.github.io/hisi-riscv-qemu/>**
>
> 文档按 [Diátaxis](https://diataxis.fr/) 组织，源在 [docs/](docs/README.md)：
> 上手教程 [docs/tutorials/](docs/tutorials/01-install.md)、操作指南 [docs/how-to/](docs/how-to/)、
> 参考 [docs/reference/](docs/reference/)（内存映射 / 机器模型 / 外设矩阵 / xlinx ISA / ROM 桩 / 验证覆盖）、
> 解释 [docs/explanation/](docs/explanation/)（设计取舍 / 中断控制器 / 已知边界）；
> 变更见 [CHANGELOG.md](CHANGELOG.md)，规划见 [ROADMAP.md](ROADMAP.md)。

## 快速开始

```bash
# 1. 安装构建依赖（Debian/Ubuntu，需 sudo）
bash scripts/setup-deps.sh

# 2. 克隆固定版 QEMU、注入 WS63 板卡、构建 qemu-system-riscv32（~10–20 分钟）
bash scripts/build.sh
#   产物：./qemu/build/qemu-system-riscv32，且 `-M help` 含 "ws63"

# 3. 运行仓库自带的预构建 C SDK 固件（默认 tests/csdk/dma.elf）
bash scripts/run.sh
bash scripts/run.sh tests/csdk/adc.elf
#   退出 QEMU：Ctrl-A 然后 X

# 4. 一键跑仓库内 C SDK 外设样例回归
bash scripts/csdk-test.sh

# 可选：运行 ws63-rs Rust 固件做扩展验证
cd ../ws63-rs && cargo build -p uart_hello --release && cd -
WS63_RS=../ws63-rs bash scripts/smoke-test.sh
```

Rust 固件路径始终也可以显式传给 `run.sh`：

```bash
bash scripts/run.sh ../ws63-rs/target/riscv32imfc-unknown-none-elf/release/uart_hello
SEMIHOST=1 bash scripts/run.sh ../ws63-rs/target/riscv32imfc-unknown-none-elf/release/semihost_selftest
```

寄存器级 qtest 免启动固件，直接驱动 GPIO/UART/timer/INTC/DMA 模型：

```bash
bash scripts/qtest.sh
```

直接调用：

```bash
./qemu/build/qemu-system-riscv32 -M ws63 -nographic -serial mon:stdio \
    -kernel <firmware.elf>
```

调试 / 追踪（看到未建模外设的访问、guest 错误）：

```bash
DEBUG=1 bash scripts/run.sh <firmware.elf>   # 写 qemu.log
# 或： -d int,unimp,guest_errors -D qemu.log
```

## 它做了什么

| 方面 | 说明 |
|------|------|
| 机器 | `-M ws63`：单 **RV32IMFC** hart（命名 CPU `-cpu ws63` = I/M/F/C + Zicsr/Zcf，关 A/D、无 MMU；为默认 CPU 型号），WS63 内存映射，复位到 ELF entry |
| UART0/1/2 | **自定义 HiSilicon UART**（非 16550）；TX 直接输出到 `-serial` |
| TIMER ×3 | 下数计数器 + 中断（IRQ 26/27/28），周期重载 |
| GPIO0/1/2 | 输出 set/clr、输入、中断寄存器 |
| SYS_CTL0 | 时钟状态（TCXO/PLL 锁），使 `init_clocks()` 跑通 |
| 中断控制器 | 自定义 `LOCIxx` CSR + IRQ 路由；**IRQ 26–31（mie 类）+ ≥32（自定义本地中断，target/riscv 补丁）均完整投递**（timer_irq / gpio_irq 已验证；见 [中断控制器设计](docs/explanation/interrupt-controller.md)） |
| 内存 | BOOTROM/ROM/ITCM/DTCM/FLASH/SRAM，按 `ws63-rt/memory.x` 布局（见 [docs/reference/memory-map.md](docs/reference/memory-map.md)） |
| 其它外设 | `create_unimplemented_device` 吸收（I2C/SPI/PWM/…），`-d unimp` 按地址可追踪 |

## 目录结构

```
ws63-qemu/
├── src/
│   ├── hw/riscv/ws63.c                            # 机器模型 + 自定义 UART 设备（注入 QEMU 树）
│   ├── target/riscv/insn_trans/trans_xlinx.c.inc  # xlinx 自定义 ISA 解码（注入）
│   └── tests/qtest/ws63-test.c                    # 寄存器级 qtest（注入 QEMU 树）
├── patches/                          # 对既有 QEMU 文件改动的 patch-series，**按版本分目录**
│   ├── v10.0.0/                       #   当前默认基线（git format-patch 生成）
│   │   ├── 0001-target-riscv-*.patch  #     CPU 型号 + 本地中断(≥32) + xlinx 解码 hooks + ROM 拦截
│   │   ├── 0002-hw-riscv-*.patch      #     注册机器（meson 源集 + Kconfig + trace-events）
│   │   └── 0003-tests-qtest-*.patch   #     注册 ws63-test（qtests_riscv32）
│   ├── v11.0.1/                       #   最新稳定主版（+ 0004 hw/→hw/core/ + 0005 修上游 PMU 回归）
│   ├── v10.2.3/                       #   最新 10.2.x，已移植（同结构 + 0004 适配 ws63.c）
│   └── v9.2.4/                        #   上一基线，仍维护（同结构 + 0004 适配 ws63.c 的旧 API）
├── scripts/
│   ├── setup-deps.sh        # 安装构建依赖（含 libslirp-dev）
│   ├── build.sh             # 克隆 QEMU@$QEMU_TAG + 拷源文件 + apply patches/$QEMU_TAG/ + 构建（幂等）
│   ├── run.sh               # 运行固件 ELF（DEBUG/ICOUNT/NV/SEMIHOST 开关）
│   ├── smoke-test.sh        # 启动 ws63-rs Rust 固件冒烟（进阶验证）
│   ├── csdk-test.sh         # C SDK 外设样例回归
│   └── qtest.sh             # 寄存器级 qtest 运行器
├── tests/csdk/              # 预构建 C SDK 样例 ELF + flash/NV 镜像（测试夹具）
├── docs/                    # 按 Diátaxis 组织:tutorials/ how-to/ reference/ explanation/(入口 docs/README.md)
└── qemu/                    # 由 build.sh 克隆的 QEMU 树（.gitignore）
```

## 真值来源

- 内存布局：`ws63-rs/ws63-rt/{memory.x,layout.ld}` 与 fbb_ws63 C SDK 板级配置相互对齐。
- 外设基址 / 寄存器：`ws63-rs/ws63-pac/ws63-svd/WS63.svd`（svd 现为 ws63-pac 的嵌套子模块）。
- 寄存器行为：fbb_ws63 C SDK `hal_*_regs_def.h` + `ws63-rs` HAL 交叉验证。

## 许可

`src/hw/riscv/ws63.c` 派生自 QEMU 代码风格，按 **GPL-2.0-or-later** 授权（与 QEMU 一致）。

### WS53 UART 外部 TX 引脚模型

显式开启 `ws53-uart.assume-pad-tx=on`、`ws53-uart.assume-dma-profile=on`
和 `ws53-pinctrl.assume-uart-tx=on`，8N1 字节按分频寄存器及假设32MHz时钟
输出逐位电平。该开关只启用TX；RX由下方独立开关启用。其他外部帧格式及
发送中改格式明确报错。既有
内部回环继续使用独立的 `char-ns` 时序假设，默认行为保持。非零分频、精确
时钟来源、模拟边沿和负载仍不能由模型证明。

路由为 H1/MGPIO12(mode2)、H0/MGPIO17(mode2)及MGPIO8(mode1)、
L0/AGPIO1(mode1)。各 pad 独立选择信号，允许 H0 同时驱动两个已复用 pad，
对应 `hisi-registers` 的 `ws53-uart-pad-route-hil.json` 三轮实测。pad 的
`uart-tx-bytes-N` 与 `uart-tx-hash-N` QOM属性记录整帧期间连接有效的虚拟对端
字节数/FNV1a64；中途切换、复位或数字争用的帧不计成功。GPIO数字争用仍可
单独观测，不能据此推断电流。TX场景不会自动开启RX串行采样模型。

`ws53-pinctrl.uart-console-profile=on` 是明确的 RAM 继承配置：M17/M18 mode2、
AG1/AG2 mode1，其余 mode0；不是芯片复位默认值。用于主工程的
`uart-pad-probe` 同一ELF对照。所有 pad 字节数/校验和及八对pad配置和GPIO
方向恢复都必须检查，不能只看控制台成功标记。

`uv run --script scripts/ws53_uart_pads_test.py --qemu <qemu-system-riscv32>`
覆盖逐位数据/停止位、双pad输出、复用切换、内部回环隔离、暂停、复位、争用
及不支持配置；已接入 `test-ws53.py`。原有 H1/H0/L0 复位和UART DMA模型独立。

### WS53 UART RX 数字输入模型

`ws53-uart.assume-pad-rx=on` 与 `ws53-pinctrl.assume-uart-rx=on` 显式启用
8N1接收器。按分频寄存器及假设32MHz时钟，在起始位中心检查低电平、随后逐位
采样数据和停止位。正常/帧错误数据送入原有FIFO、LSR、IRQ和DMA请求路径，
不增加绕过寄存器行为的“成功”返回。起始毛刺、输入使能/复用断开、
无pull时外部驱动丢失、时钟丢失及复位取消未完成帧；有pull维持电平时继续
数字采样。这些取消策略属于模型约定，尚无相应电气实测。

RX路由为UART0/AGPIO4(mode2)、UART1/MGPIO18(mode2)或MGPIO9(mode1)、
UART2/AGPIO2(mode1)。要求输入缓冲使能，且外部驱动或内部pull建立数字电平；
悬空pad不凭数字默认零制造输入字节。UART1同时选中两个RX pad时，输出明确
诊断并断开输入，既不猜测OR/AND，也不假设优先级。恢复单一路由后需要新的
有效起始边沿。内部回环期间外部pad输入被隔离。

开启RX模型会禁止原字符后端直接向FIFO注入字节，测试须通过
`/machine/pinctrl` 的 `external-level` / `external-drive` 数字输入驱动实际pad。
也可额外开启 `ws53-uart.assume-rx-peer=on`，将字符后端输入交给独立8N1
发送器，再经固定板端 pad（UART0/AGPIO4、UART1/MGPIO18、UART2/AGPIO2）
的复用和输入使能进入RX采样器。`rx-peer-baud`默认115200，可设1200..4000000，
独立于芯片分频器。64字节等待队列通过字符后端背压保序；目标时钟或外设复位
不会暂停发送器，整机复位则清空虚拟对端。不能同时用qtest在同一pad注入另一个
外部驱动；不模拟流控、电气争用或精确物理时序。RX及peer默认关闭，旧场景保持兼容。

`scripts/ws53_uart_peer_test.py` 检查三个对端、输入选择、独立时钟/复位及超过
队列容量的收发。主工程 `test-ws53-uart-host.py --mode irq-pad` 使用现有UART2
IRQ RAM镜像，7个字节全部经peer与AGPIO2采样，检查两次IRQ42通知及取消后读取。
同镜像三轮实板对照仅确认继承配置的UART2正常接收；UART0/1物理RX及错误路径
仍待验收。只读 `rx-peer-frames` 统计发送完毕的帧，区别于 `pad-rx-frames`。

`uv run --script scripts/ws53_uart_rx_pads_test.py --qemu <qemu-system-riscv32>`
覆盖四组路由各256字节、数据IRQ及屏蔽恢复、帧错误读清、FIFO满/溢出、起始
毛刺、复用/输入/驱动断开、内部回环隔离、时钟/复位恢复、双RX歧义、非法帧
格式、接收中改分频拒绝及禁止后端旁路；纳入完整 `test-ws53.py`。
`pad-rx-frames` / `pad-rx-aborted` 为只读QOM统计。没有RX电气、过采样抗扰、
Break、其他帧格式或精准分数分频调制的验收结论。
