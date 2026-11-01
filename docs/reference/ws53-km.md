# WS53 KM 明文密钥装载模型

`-M ws53` 在 `0x52022000..0x52023fff` 映射独立 `ws53-km` 设备。
布局来源为 hisi-registers `6cda2bb`、SDK `163d9150`。默认不启用行为；访问会输出
明确诊断。测试显式设置 `-global ws53-km.assume-profile=on`。

这是应用核 clear-loader 子集，包含八个 cipher 槽、两个 HMAC 槽、REE/TEE 槽命令、
暂存字、目标选择、易失性工作密钥权限、START/错误/完成状态及释放。模型把实际
输入字节复制到私有槽存储，检查槽号、归属、算法引擎、长度编码、四字暂存与权限。
支持 AES-128/192/256、SM4、HMAC 使用的四/八段装载，不提供加密或 HMAC 计算。
common/root 路线、OTP、Flash key 和未审计控制明确拒绝；不是完整安全子系统。

以下为显式模型假设，不能当作实板事实：

- claim 原子生效，owner1/2 分别为 REE/TEE；clear-loader 仅接受 REE 槽。
- START 时锁存四个小端字，5µs 虚拟时间后复制一段并产生 raw bit14。
- ACK 仅清 bit14，保留 index；其他 pending 位的整字重放被拒绝。
- flush 5µs 后释放 owner/valid，**故意保留旧字节**，不承诺物理擦除。
- 停钟暂停装载与释放，恢复后重新等待完整模型时延。复位取消待处理操作、清模型
  私有存储，不能证明实际复位值、擦除、总线排空或厂商跨核交接。

IRQ61 连接到机器中断控制器，但只在 `-global ws53-km.route-clear-irq=on` 下输出
clear 完成事件。尚无来源可据以实现完整 IRQ enable/安全路由寄存器，故不编造该 MMIO。
qtest 检查事件保持和 ACK 后撤销；Rust fixture 仅轮询，不声明验证了 KM ISR/async。

`/machine/km` 的模型诊断：

| 接口 | 用途 |
|---|---|
| `debug-keys` / `debug-slots` / `debug-staging` | 观察合成测试字节、owner/valid/engine/size、暂存区；不是 guest 读密钥接口 |
| `submitted-blocks` / `completed-blocks` / `released-slots` | 验证数据确实经过提交、完成、释放 |
| `reads` / `writes` / `rejections` | 验证故障后 HAL 不再访问、不以未知 MMIO 返回零冒充实现 |
| `inject-loader-error` / `inject-controller-error` / `inject-raw` | 不解释未知位含义的外部故障注入；raw 注入不模拟实际 IRQ 电路 |
| `fault-after-submissions` | LOAD_STALL / LOSE_OWNER 在累计提交数超过此值时生效，默认0 |
| GPIO `clock[0]` | 外部受控模型时钟，不是实际芯片时钟树配置 |
| GPIO `fault[0..7]` | 装载停滞、拒绝claim、完成时丢失owner、ACK不清、flush停滞、flush timeout、flush fail、flush后owner不清 |

断言只使用公开生成的 fixture 字节，不向日志输出真实设备密钥。
非法宽度、未知寄存器、只写暂存读取、忙时修改/重复提交、无权限和无归属提交均明确诊断。

```sh
uv run --script scripts/ws53_km_test.py --qemu ./qemu/build/qemu-system-riscv32
uv run --script scripts/test-ws53.py --qemu ./qemu/build/qemu-system-riscv32
```

寄存器测试覆盖144组cipher、36组HMAC、384段复制、20组双CPU归属冲突、8类故障、
装载/释放停钟、两种在途复位、11种无效访问、13种非法提交及7种忙/停钟访问。
主工程 `examples/ws53-km-model` 另运行实际生产 HAL 的357个 CPU 场景。
两者均不替代 RAM、Flash 或冷启动验收，也不证明 AES/SM4/HMAC 算法结果。
