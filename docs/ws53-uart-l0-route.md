# WS53 UART L0 应用核 IRQ 路由

SDK `fbb_ws53` 的提交 `163d9150ab94a02d09a05f623351f9facee3025e` 中，
`drivers/chips/ws53/porting/uart/uart_porting.c` 的 `uart_port_config_pinmux`
在 `CONFIG_UART_MULTI_CORE_RESUME` 下向 `0x57000048` 写完整 32 位字 `1`，
注释为“l0中断只报A核”。这是本模型唯一接受的 guest 路由命令。
不推断可读性、其他数值、硬件复位值或控制核的中断目的地。

## 显式模型配置

`-global ws53-uart-l0-route.assume-profile=on` 启用命令到 IRQ42 的路由门禁。
模型起始及 `system_reset` 后要求重新发命令；该初始状态是测试政策，
不是芯片复位值。命令不会清 UART FIFO、IER 或状态，不会改变 UART0 IRQ39
或 UART1 IRQ41。重复命令幂等地保持已路由状态，并累加诊断计数。

未启用该选项时，既有 UART2 直通 IRQ42 行为保持，路由命令访问仍拒绝。
此兼容模式不证明应用已执行中断归属交接。两种模式的所有 guest 路由读取、
错误宽度/对齐/地址/值均返回 `MEMTX_ERROR`，不返回伪造的路由读回值。
CPU 测试核对实际访问陷阱，不能用 qtest 返回的零判断访问成功。

QOM 对象 `/machine/uart-l0-route` 提供只读 `routed`、`source-pending`、
`blocked` 和 `commands`。命名输入 `block-route` 是外部模型故障注入，
会阻断路由后的投递，并跨 guest 路由命令和 reset 保持，直到测试显式清除。
它不是 guest 寄存器或硬件故障编码。控制核投递、跨核仲裁与物理电气行为均未建模。

## 验证

```sh
uv run --script scripts/ws53_uart_route_test.py \
  --qemu /path/to/qemu-system-riscv32 --output /tmp/ws53-uart-route-test
```

输出目录必须不存在。测试覆盖 UART 实际内部 loopback/FIFO/IER 到路由/IRQ42、
排队数据保留、两只 peer UART、重复命令、两轮复位/故障恢复、默认兼容模式、
14 种非法 guest 事务与 7 次真实 TCG 访问陷阱。主 `test-ws53.py` 同时调用该场景。
该模型测试不等同于 Rust Flash 应用、真实 Flash 烧录、串口 pad 电气或断电启动验收。
