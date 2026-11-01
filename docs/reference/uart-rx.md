# WS63/BS2X UART RX 模型

`ws63-uart` 由 WS63 和 BS2X 机器复用。此增量核对 WS63 SDK v151 的
`hal_uart_v151_regs_def.h`：`intr_en` 的接收使能为 bit 2，`line_status`
的数据可用为 bit 5，发送就绪为 bits 6/7；不能采用标准 16550 位号。
既有 WS63 HAL blocking `read_byte` 使用这条状态/数据路径。

模型在数据进入、消费、使能和 FIFO 门限变化时重新计算 IRQ。屏蔽通知保留字节，
再次使能可以立即通知；读数据释放 chardev 背压。FIFO RX reset 清队列，设备 reset
同时清配置和 IRQ。`fifo_ctl` 的接收门限遵循 SDK 的一字节、1/4、1/2、容量减二规则。
接收计数别名 0x4c 尚未核实，访问时明确打印 guest-error，不能作为验收捷径。

`-global ws63-uart.rx-fifo-size=16` 选择合成容量。默认 1 保留旧模型的容量；允许
16–2048 的二次幂，以便检查缓冲边界。它不是芯片实际 FIFO 深度的声明。
FIFO 未开启时容量为 1；已有数据不会因调整门限而丢弃。

独立回归：

```sh
uv run --script scripts/ws63_uart_rx_test.py --qemu ./qemu/build/qemu-system-riscv32
bash scripts/qtest.sh
```

前者检查 WS63 的容量 1/16 与 BS20/BS21/BS21E/BS22 的容量 1，每个配置覆盖三路：
状态位、RX 专属使能、屏蔽保留、重新使能、超容量输入/背压、FIFO reset、设备 reset；
WS63 还检查 controller IRQ，容量 16 检查门限。父仓另用真实 Rust async 驱动验证
IRQ、取消及重用；通用 Future 生命周期的单元测试归 HAL。

尚未建模或验收：物理 FIFO 深度、输入溢出、电气/波特率、字符超时、线路错误、
真实 TX FIFO 排空。发送仍直接写 chardev。原 FIFO_STATUS 的简化约定未在此升级为
物理证据。本模型不能证明实板接收、DMA 或全部 UART 中断类型正确。
