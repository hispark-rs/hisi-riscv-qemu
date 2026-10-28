# WS53 SFC 命令引擎与合成 NOR

此模型只覆盖 SFC v150 的 CPU 命令通路。NOR 是独立的 **4 MiB 合成测试对端**，
不模拟实板 GD25 型号，不使用厂商 Flash 内容，不提供真实 NV、保护位、时序、
缓存一致性或 DMA 排空资格。默认关闭；未资格访问返回 `MEMTX_ERROR` 并记录
`guest_errors`，CPU 的读写应产生访问异常，而不是读零或写入后假装成功。

## 显式启用与寄存器范围

```text
-M ws53 -global ws53-sfc-command.assume-profile=on
```

`assume-profile` 为 qdev 启动属性，realize 后不能通过 QMP 修改，包括同值设置。
它与 `flashboot-handoff=on` 互斥。合成数组没有映射到 CPU 地址空间；启用时
`0x400000..0x7fffff` 是明确拒绝的 XIP 窗口，不能另有一份悄悄分离的存储。

`/machine/sfc-command` 在 `0x53000000` 提供下列 32 位对齐访问：

| 偏移 | 寄存器 | 支持范围 |
| --- | --- | --- |
| 0x300 | CMD_CONFIG | START/CS/ADDR/DUMMY/DATA/RW/LEN/IF 字段，启动时校验形状 |
| 0x308 | CMD_INS | opcode 低 8 位；DDR 和其他位拒绝 |
| 0x30c | CMD_ADDR | 布局低 30 位；数组命令必须在合成容量内 |
| 0x400..0x43c | 16 个数据寄存器 | 共 64 字节，小端 CPU 字节序 |

只支持 CS1、standard SPI、零 dummy。无 DATA 时忽略 LEN，符合 SDK 的零长度
减一编码；无 ADDR 时不使用残留地址。START=0 可以暂存配置，START=1 才提交完整
检查。控制器忙时拒绝全部写入；读取已有 DATA 不清状态，也不保证内容属于新命令。
数据读取只替换有效响应字节，其他字节保留旧值。

BUS_CONFIG、BUS_DMA、FAPC、AES、IRQ、软复位及其他未实现寄存器全部拒绝。
特别是遗留 `0x120/124/128/12c/130`，固定 WS53 SDK 没有相应来源；没有推测应用核
SFC IRQ 路由。这里也不提供 BUS_DMA_START 的“恒为零”占位。

### 可选的 configured snapshot 只读观察

普通命令 profile 的上述拒绝范围保持不变。另有显式合成启动状态，可供未来
RAM 中的只读命令客户端检查前提；这不是实板 FlashBoot 所有权交接证明：

```text
-M ws53 -global ws53-sfc-command.assume-profile=on \
  -global ws53-sfc-command.configured-handoff=on \
  -global ws53-sfc-command.snapshot-global=0x0 \
  -global ws53-sfc-command.snapshot-dma=0x0 \
  -global ws53-sfc-command.snapshot-clock=0xd
```

三个原始32位值必须全部显式提供，缺失、负数、尾随字符或溢出均拒绝启动；关闭
`configured-handoff` 时不得提供这些值。所有选项只能在 realize 前设置，运行中
通过 QMP 设置同值也拒绝。示例的零和 `0xd` 是人为测试输入，不是芯片复位值。
本 profile 仍要求 `assume-profile`，仍与 `flashboot-handoff` 互斥，不提供 XIP。

| 地址 | 观察范围 |
| --- | --- |
| `0x53000100` GLOBAL_CONFIG | 仅32位对齐读，原始32位全部保留 |
| `0x53000240` BUS_DMA_CTRL | 仅32位对齐读，原始32位全部保留 |
| `0x52000570` XIP_SFC_CLOCK | 同址16/32位读；半字是字的低16位 |

这些读取不清位、不执行初始化、不创建 DMA 事务。配置写（包括同值、半字 RMW）
全部返回 `MEMTX_ERROR`；DMA其他寄存器、IRQ、BUS映射、FAPC/AES继续拒绝。
时钟窗口仅占4字节，不覆盖相邻 `0x52000574` 的独立 QSPI 时钟模型。错误宽度及
偏移在各窗口内明确拒绝；跨多个外设窗口的总线访问不是一个原子配置事务。

提交 CMD_START 时检查：DMA_START bit0 不得为1，时钟 ENABLE bit0 必须为1，
SOURCE_CONFIG bits3:1 只能是0或6，GLOBAL_CONFIG 的 mode bit0 和
flash_addr_mode bit2 必须为0。mode编码1（SDK枚举Mode3）和四字节地址在本
合成通路中未支持。失败不改变命令、数据、NOR或使能状态。其余快照位仅供原样
观察；WP、rd_delay、sample_test不提供真实写保护或采样时序效果的资格。
0和6是SDK源码出现的配置编码，**没有据此计算物理频率**；命令10µs仍是独立假设。

`system_reset` 重放所选快照，同时沿用原控制器取消未提交命令、外部NOR保留已
提交工作的规则。重放是合成启动场景，不是硅POR/retention；DMA_START=1始终保持
忙，不会因复位或读取变0，也不声称已取消或排空任何RAM master。读取快照成功、
CMD_START=0、DMA_START=0均不证明资源独占、无并发XIP、WIP清零或cache/drain。

模型原有NOR写能力保留，新增只读QOM计数 `mutating-commands-started` 统计被接收的
06/04/50/01/02/20。只读CPU测试发9f/05/35/03（显式提供SR3时另发15）并检查该计数为0、NOR数组摘要和
运行态/NV/WEL全部不变。它是独立TCG客户端，不能冒充尚未运行的Rust HAL验证。
默认15/SR3继续明确拒绝。仅当另行显式提供startup属性
`-global ws53-sfc-command.snapshot-status3=0xa7` 时才允许无地址、读方向、长度1的
15命令，返回该原始u8；它只依赖 `assume-profile`，不要求configured快照。缺失不
补零，负数/溢出/畸形值拒绝，运行后不能更改。0/0xa7/0xff都是可选合成输入，不
解释任何位为具体器件功能。这个只读常量跨controller reset和合成peer power-cycle
保留；它没有可写NV副本，不宣称真实SR3的复位或持久化格式。NOR WIP期间可以读，
但15返回值不被解释为WIP，不替代05。

布局来源为 `hisi-registers` 提交 `92a3d202e70e305426c18af07158731e7e95e921`
及固定SDK `clock_recover.c:137–147`、`flashboot_ws53/startup/main.c:89–97`。
它们只提供使能/编码和混合访问宽度；没有 ready、drain、频率或安全切频协议。

```bash
uv run --script scripts/ws53_sfc_snapshot_test.py --qemu ./qemu/build/qemu-system-riscv32 --output /tmp/ws53-sfc-snapshot-new
```

该测试包含启动缺项/畸形输入、QMP运行中变更、未知高位保留、两个访问视图、全部
未支持source编码、CMD与NOR WIP区分、两轮复位及真正TCG的cause5/7、mepc、mtval、
rd/内存哨兵和有界命令轮询。软件通过不改变既有候选镜像或正式CI构建资格。

## 命令和数据效果

| opcode | 形状 | 合成对端行为 |
| --- | --- | --- |
| 0x9f | 读 3 字节、无地址 | 返回固定字节 `7f 53 01`，不是实板 JEDEC ID |
| 0x05 / 0x35 | 读 1 字节、无地址 | SR1（含 WIP/WEL）/SR2；读不清状态 |
| 0x15（仅显式seed） | 读 1 字节、无地址 | 返回 `snapshot-status3` 原始u8，不解释位语义；未提供则拒绝 |
| 0x06 / 0x04 | 写方向、无数据/地址 | 设置/清除 WEL，同时取消 volatile-enable |
| 0x50 | 写方向、无数据/地址 | 设置 volatile-enable，清 WEL |
| 0x01 | 写 2 字节、无地址 | 需要 WEL 或 volatile-enable；提交状态更新 |
| 0x03 | 读 1..64 字节、有地址 | 读取真实合成数组字节 |
| 0x02 | 写 1..64 字节、有地址 | 需要 WEL，提交后逐字节 `old & new` |
| 0x20 | 写方向、无数据、有地址 | 需要 WEL，提交后整 4 KiB 扇区变为 FF |

数组初始全 FF，页为 256 字节、扇区为 4 KiB。跨页编程、不对齐擦除、越界、
缺少使能、未知 opcode 和不匹配命令形状都原子拒绝，不清 WEL、不改部分字节。
NOR 忙时只允许 0x05/0x35及已显式配置的0x15。读取可跨页，但不能越过 4 MiB 边界。

0x01 仅允许 SR1 的 `0x1c` 位和 SR2 的 `0x02` 位。**这些是合成语义**：
SR1 BP 非零保护整个数组；SR2 bit1 仅储存，不使能 Quad。其他状态位、锁定、CMP、
真实器件分区保护与 OTP 命令都未实现。检查状态位掩码不等于还原完整实物状态。
WEL 路径更新运行态及模型 NV 两字节；volatile-enable 路径只更新运行态。
不含磁盘后端，NV 只表示本次模型进程中可经显式对端掉电恢复的状态库。

## 两个完成事件与恢复

全部时间是模型假设：控制器命令 10 µs；自命令完成开始，页编程另需 200 µs，
扇区擦除另需 1 ms，状态写另需 100 µs。控制器完成先清 CMD_START；对端随后保持
WIP，直到另一个事件提交数据并清 WIP。提交前的数据仍是旧内容。不能由 START=0
推导 NOR 已完成，更不能推导整个系统的 posted writes 或缓存已排空。

系统 `system_reset` 只复位控制器：清命令/地址/数据寄存器、取消尚未发送完成的
命令。已经发给 NOR 的操作继续，WEL、运行态/NV 状态和数组保留。计数器及外部
故障输入也保留。这是选定的模型复位边界，不声称真实板级复位拓扑已验收。

可控 GPIO 输入（均为测试输入，不是 WS53 寄存器）：

- `controller-stall`：冻结控制器剩余时间，解除后继续。
- `peer-stall`：冻结 NOR 剩余提交时间，状态读取仍可工作。
- `peer-power-cycle`：上升沿取消对端在途事务及未完成控制器交换、清使能并恢复
  NV 状态；保留已提交数组。取消写按原子未提交处理，不模拟真实断电半写。

只读 QOM 提供 `commands-started`、`commands-completed`、`rejections`、
`peer-operations`、`peer-commits`、`peer-power-cycles`、`peer-busy`、`peer-wel`、
`peer-volatile-enable`、`peer-sr1`、`peer-sr2`、`peer-nv-sr1`、`peer-nv-sr2` 及全数组
`peer-sha256`。这些是测试观察接口，不暴露给固件，也不伪造芯片错误/完成寄存器。

## 来源、实现选择和验证

寄存器来源为 SDK 提交 `163d9150ab94a02d09a05f623351f9facee3025e`：
`hal_sfc_v150_regs_def.h:188–198,234–258,325–348`、
`hal_sfc_v150_regs_op.c:21–65`、`sfc_porting.c:21–43`。
这些证明命令布局、CPU 数据缓冲区和轮询用法，不证明 NOR 的时序、NV 或复位规则。
`wait_config()` 只等控制器 START；`wait_ready()` 则发 0x05 后检查 WIP。
SDK 和已审 FlashBoot 的若干调用会忽略等待错误，不能用 init 成功代替独占证明。

选择独立合成对端而不直接复用固定 QEMU v10 的 m25p80：后者 `RDSR` 不提供本轮
所需的延时 WIP，page-program 路径立即逐字节修改数组。直接复用会把控制器完成
和内部编程提交混合。将来接 SSI/具体器件必须另做语义验收，不能沿用本模型资格。

```bash
uv run --script scripts/ws53_sfc_contract_test.py --qemu ./qemu/build/qemu-system-riscv32 --output /tmp/ws53-sfc-contract-new
uv run --script scripts/ws53_sfc_cpu_test.py --qemu ./qemu/build/qemu-system-riscv32
```

前者独立对照命令数据、完整数组摘要、两阶段完成、NV/volatile、拒绝原子性及复位；
后者用 TCG 的真实 load/store 验证默认拒绝、未实现 MMIO/XIP 和错误宽度产生
cause5/7，异常返回、rd 和内存哨兵保持正确。测试通过只属于该合成软件模型。
