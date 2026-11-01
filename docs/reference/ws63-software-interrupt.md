# WS63 软件通知源

`SYS_CTL1` 的 0x44000150/154/158/15c 分别是 EN、SET、CLR、STS。
布局和 bit0–3 来自 WS63 PAC `sys_ctl1`；SDK `chip_core_irq.h` 将四条通知映射到
LOCAL_INTERRUPT0+10..13，即 cause 36..39。它不是标准机器软件中断 cause 3。

`ws63-softint` 在原 TCXO 大窗口上覆盖这 16 字节；不接管其他 SYS_CTL1 或 TCXO
寄存器，不为 BS2X/WS53 自动套用该映射。SET/CLR 为逐位自清命令，STS 只读。
模型将置位状态和使能分开：禁用屏蔽投递而保留 pending，重新使能重新计算 IRQ；
reset 清使能、pending 和 IRQ。保留 pending 的语义是此模型的明确契约，仍需逐项
实板验证，不能由寄存器命名或仿真通过推导全部硅片时序。

此前这些地址只是 TCXO shadow 中的普通数值。RTOS 请求软件中断后无法立即交接，
只能等下一次 TIMER IRQ；7 ms 原生任务与 11 ms Embassy 任务因此交替拖延。
模型修正不改变 RTOS 调度规则，也不降低父仓共存验收门槛。

```sh
uv run --script scripts/ws63_softint_test.py --qemu ./qemu/build/qemu-system-riscv32
```

该检查覆盖四条输出的隔离、命令自清、重复/零写入、屏蔽保留、只读拒写、reset
和 TCXO 窗口不被覆盖。父仓另外运行真实 RTOS/Embassy 程序检查 cause36 的实际
投递和上下文切换。纯寄存器测试不证明 CPU trap、优先级、嵌套或物理同步延迟。
