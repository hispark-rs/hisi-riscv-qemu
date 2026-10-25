# WS53 GPIO SW_CTL 的显式观察模型

SW_CTL 是 GPIO v150 的 `+0x08` 寄存器，GPIO0 地址为 `0x5700c008`。
SDK `163d9150ab94a02d09a05f623351f9facee3025e` 的
`drivers/drivers/hal/gpio/v150/hal_gpio_v150_regs_def.h:95–106` 声明它为
`volatile uint32_t`，`hal_gpio_v150_regs_op.h:116–138` 提供 getter 和位 RMW setter。
因此硬件寄存器是 RW，本模型的观察范围不能解释为硬件 RO。
当前没有足够证据解释每位选择编码，也没有已知的通用硅片复位值；特别是不能把
旧实板读到的零解释为“选择软件控制”。

## 显式配置

```sh
qemu-system-riscv32 -M ws53 \
  -global ws53-gpio.assume-source-control=on \
  -global ws53-gpio.source-control-value=0x87654321
```

两个参数共同建立模型观察前提。默认关闭，读取 SW_CTL 会记录 guest-error 并返回
`MEMTX_ERROR`，不能用返回的零冒充已实现。非零初值而未打开前提时，模型创建失败。
启用后三个 GPIO bank 各输出一条明确 notice，包含 bank 宽度 23、12、3。
新 runner 必须精确许可这些 notice，其余 guest-error 仍是失败。

32 位读返回完整配置值，不按 bank 引脚数量截断，不改变 latch、OEN、EN、MASK、
edge latch、raw interrupt、IRQ 输出或 pad 状态。观察值不参与已存在的数字 GPIO
路由；即便非零值下功能测试通过，也不证明这个数值的硬件选择含义。
系统复位恢复已配置观察值，不称其为真实硬件复位值。

本轮模型仅支持对齐 32 位 GPIO MMIO。byte、halfword、64 位和未对齐访问明确
失败，64 位写不会拆成对 SW_CTL 和邻接 INT_EN 的两个写操作。所有 guest SW_CTL
写（包括写零）都失败且不改状态。真实 TCG 测试验证 load/store access fault，
而不是只依赖会丢弃 `MemTxResult` 的 qtest 返回值。
这些错误是让未支持软件路径停止的模型政策，不是硅片访问异常、返回电平或总线
等待时长的预测。

## 仅用于模型的来源变化输入

已启用 profile 后，QMP 可以注入一次完整的观察值变化：

```json
{"execute":"qom-set","arguments":{"path":"/machine/gpio0","property":"source-control-observation","value":7}}
```

`qom-get` 同名属性可读当前值。属性只接受 u32，并要求显式 profile；它既不模拟
客体写 SW_CTL，也不切换任何硬件来源。变化只影响该 bank 的观察值，不能改变其他
GPIO 状态。系统复位撤销该临时注入并恢复 `source-control-value`。这用于后续
应用前后快照变化的失败测试，不能成为 HIL 的隐式预初始化步骤。

## IRQ34 和取消范围

GPIO0 继续连接本地 IRQ34，触发仍由 EN、OEN、edge/level、MASK 和 EOI 决定。
没有增加未经证实的 core-route 或 GPIO 时钟初始化。测试在 Flash handoff 的
CCORE=0 下验证来源观察、mask 保留 edge、unmask 投递、disable 阻止投递、按位
EOI 取消和 peer pending 保留；GPIO 不需要温度域的释放动作。

这些 bank 级回归证明模型能够表达 async 等待和取消所需的条件；它们没有运行
新的 Rust Flash async fixture，也不证明 waker、guard 或真实 ISR 的应用逻辑。
完整的 Pending→IRQ→wake→repoll、八次 IRQ 和错误恢复仍需新的应用 CPU 场景，
实板必须另有同次 UART、DSLogic 和 Flash/复位/冷启动证据。

## 回归入口

```sh
uv run --script scripts/ws53_gpio_source_test.py \
  --qemu /path/to/new-qemu-system-riscv32 --output /path/to/new-results
```

覆盖零、非零和全一观察值；三个 bank 的完整数据保留；读取无副作用；明确注入与
两轮复位；未启用访问、错误宽度、guest 写及非法 QOM 值拒绝；IRQ34 的按位取消；
两个真实 TCG 程序的精确 mcause/mepc/mtval；以及原三 bank GPIO 回归。
结果与原始诊断、CPU 程序和逐条 qtest 记录一同保存。无硬件、SDK 二进制或厂商
固件参与执行。
