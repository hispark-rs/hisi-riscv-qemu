# 调试与追踪

本指南讲怎么在 ws63-qemu 上调试固件、以及怎么追踪/记录仿真器的内部行为。

## GDB 源码级调试

QEMU 内置 gdbstub 对本机器开箱即用。透传 `-s -S`（= 在 `:1234` 起 gdbstub 并冻结在复位）：

```bash
bash scripts/run.sh fw.elf -s -S
# 另一终端:
riscv32-unknown-elf-gdb fw.elf   # 或 gdb-multiarch
(gdb) target remote :1234
(gdb) break main
(gdb) continue
```

> 注意：GDB 的反汇编器**不认识 xlinx 自定义指令**，落在 ROM/xlinx 区的指令会显示为未知。源码级
> 断点/单步/查看变量在标准指令上正常工作。xlinx 指令是什么见 [xlinx ISA 参考](../reference/xlinx-isa.md)。

## 追踪与日志

```bash
DEBUG=1 bash scripts/run.sh fw.elf   # 加 -d int,guest_errors,unimp -D qemu.log
```

或手动给 QEMU 传 `-d`：

- `-d unimp`：打印对**未建模/兜底**外设的访问（按地址定位）。哪些是兜底见 [内存映射](../reference/memory-map.md)。
- `-d int`：打印每次中断投递（确认 IRQ 号/向量）。
- `-d guest_errors`：guest 侧错误访问。
- `-D qemu.log`：把上述写入文件。

## WS63 模型的 trace 事件

WS63 模型用 QEMU 正规 trace 事件暴露关键行为：

```bash
-d trace:ws63_gpio_*    # GPIO 输出变化
-trace ws63_dma_xfer    # DMA 搬运（也可单独开启）
```

| 事件 | 含义 |
|------|------|
| `ws63_gpio_set` / `ws63_gpio_clr` | GPIO 输出脚拉高 / 拉低 |
| `ws63_dma_xfer` | DMA 通道搬运一次 |

Rust 进阶冒烟里的 `smoke-test.sh` 即据 `ws63_gpio_*` 断言 blinky 的 GPIO 翻转。

## 典型排查路径

- **固件大量 illegal instruction**：`DEBUG=1` 看 `qemu.log` 里陷阱 pc 的落点。落在 ROM 区（`0x109000..0x14C000`）
  是预期会被 `ws63_rom_call` 拦截的（见 [ROM 桩目录](../reference/rom-stubs.md)）；落在别处才是真问题。
- **中断没投递**：`-d int` 看是否有对应 IRQ 号的投递行；中断机制见 [中断控制器设计](../explanation/interrupt-controller.md)。
- **访问到未建模外设**：`-d unimp` 按地址定位，对照 [内存映射](../reference/memory-map.md) 看是哪个外设。

## QEMU 官方文档

- [GDB usage（gdbstub）](https://www.qemu.org/docs/master/system/gdb.html) —— `-s -S`、`target remote` 的上游说明
- [Tracing](https://www.qemu.org/docs/master/devel/tracing.html) —— trace 事件机制（`-trace` / `-d trace:`）
- [Invocation](https://www.qemu.org/docs/master/system/invocation.html) —— `-d`、`-serial`、`-D` 等命令行选项

## 相关

- 各开关清单 → [运行选项参考](../reference/run-options.md)
- 寄存器级、免启动的回归 → [运行测试](run-tests.md)
