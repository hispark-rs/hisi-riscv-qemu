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

## 命令和数据效果

| opcode | 形状 | 合成对端行为 |
| --- | --- | --- |
| 0x9f | 读 3 字节、无地址 | 返回固定字节 `7f 53 01`，不是实板 JEDEC ID |
| 0x05 / 0x35 | 读 1 字节、无地址 | SR1（含 WIP/WEL）/SR2；读不清状态 |
| 0x06 / 0x04 | 写方向、无数据/地址 | 设置/清除 WEL，同时取消 volatile-enable |
| 0x50 | 写方向、无数据/地址 | 设置 volatile-enable，清 WEL |
| 0x01 | 写 2 字节、无地址 | 需要 WEL 或 volatile-enable；提交状态更新 |
| 0x03 | 读 1..64 字节、有地址 | 读取真实合成数组字节 |
| 0x02 | 写 1..64 字节、有地址 | 需要 WEL，提交后逐字节 `old & new` |
| 0x20 | 写方向、无数据、有地址 | 需要 WEL，提交后整 4 KiB 扇区变为 FF |

数组初始全 FF，页为 256 字节、扇区为 4 KiB。跨页编程、不对齐擦除、越界、
缺少使能、未知 opcode 和不匹配命令形状都原子拒绝，不清 WEL、不改部分字节。
NOR 忙时只允许 0x05/0x35。读取可跨页，但不能越过 4 MiB 边界。

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
