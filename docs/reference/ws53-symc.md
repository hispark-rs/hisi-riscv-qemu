# WS53 SYMC 整块加密模型

`-M ws53` 的 SPACC 窗口 `0x52010000..0x5201ffff` 包含独立 SYMC 与 HASH 状态，
寄存器/描述符来源为 SDK `163d9150`、hisi-registers `6cda2bb`。默认拒绝访问；
测试显式启用 `-global ws53-symc.assume-profile=on` 和
`-global ws53-km.assume-profile=on`。这是可执行的实验模型，实板共享资源交接、
缓存与总线排空、实际时钟和冷启动均未由该模型证明。

## 数据与权限

两路 REE 通道实际读取 guest 描述符和数据，从 KM 槽读取装载的合成密钥，执行
AES-128/192/256 或 SM4，再将每块结果写回 guest RAM。支持 ECB、CBC、CTR、OFB、
CFB128 的整块双向运算；输入非空、16字节倍数，数据和表按32字节对齐且位于
mode1 DTCM。输入、输出和两张完整表必须互不重叠。两项环中只提交首项，输出写
指针必须先于输入公布，未完成时配置不可变；每块输入、计算、输出分开推进。

读取密钥时检查 cipher 槽0–7、REE owner、valid、算法引擎、长度、even/odd 半槽、
加解密用途、KM 时钟与错误。HMAC 槽及无效/未完成/已释放密钥不被接受。
KM 的装载权限现在保留在模型私有槽元数据中；guest 没有新增直接读密钥接口。

采用 QEMU AES 块实现及 SM4 公共常量；SM4 密钥扩展和轮函数执行实际算法。
没有预填“成功”结果。主机对照使用固定 PyCryptodomex 3.23.0 与 gmssl 3.2.2。
13个固定向量先验证主机参考，再用该参考比较模型：AES来自
[NIST SP800-38A 附录F](https://nvlpubs.nist.gov/nistpubs/legacy/sp/nistspecialpublication800-38a.pdf)，
SM4来自[算法描述及附录A](https://datatracker.ietf.org/doc/html/draft-ribose-cfrg-sm4-10#appendix-A)
（已过期的个人 Internet-Draft，不是 IETF 标准）。仅使用公开测试字节。

## 中断、复位和显式假设

- 两个引擎在共享 SPACC_IE 后合并到 IRQ59，保留各自 raw/enable；清一个通道或
  一个引擎的中断不会消费另一个来源。全局屏蔽、局部屏蔽不清 raw。
- owner1 表示 REE，选定位 ACK、5µs clear、1µs流水/总线阶段、两项环、coherent
  DTCM、CTR完整128位大端递增、各模式最终IV语义均是显式测试配置，不是实板定论。
- `clock` GPIO 控制虚拟处理阶段，不能冒充真实芯片时钟树。HASH 侧时钟还控制共享
  全局IRQ门，SYMC侧时钟控制本地处理/IRQ；尚未建立生产级公共门控关系。
- **已经被总线接受的读写可跨clear、系统复位或停钟完成。** 旧epoch请求不能更新
  新通道完成状态，但旧写仍可修改RAM。请求退休之前拒绝配置和重新提交；完成或
  timeout本身不构成安全回收内存/密钥的依据。
- 错误注入不编造未知cause位。无法执行的访问、配置或DMA以guest diagnostic和
  rejection计数明确暴露，不通过静默返回零伪装已实现。

`/machine/symc` 支持：

| 接口 | 内容 |
|---|---|
| `descriptors`、`bytes-read`、`bytes-written`、`transfers` | 实际描述符/数据/完成计数 |
| `pending-reads`、`pending-writes` | 总线已接受但未退休的请求 |
| `reads-after-reset`、`writes-after-reset` | clear或reset后旧epoch请求退休计数 |
| `rejections` | 明确拒绝计数 |
| `reads`、`writes` | 寄存器访问计数，用于检查终态、取消和丢弃后的访问停止 |
| `inject-{tee,ree}-{cause,status}` | 原始错误快照，不推断清除寄存器 |
| GPIO `clock[0]` | 虚拟处理时钟 |
| GPIO `fault[(channel-1)*9+index]` | 下列九类故障 |

故障索引0–8依次为clear停滞、DMA处理停滞、读错误、写错误、不产生done、拒绝claim、
丢失owner、ACK不清、保留已接受的总线请求。

## 运行测试

```sh
uv run --script scripts/ws53_symc_reference.py
uv run --script scripts/ws53_symc_test.py --qemu ./qemu/build/qemu-system-riscv32 --output symc.json
uv run --script scripts/ws53_symc_contract_test.py --qemu ./qemu/build/qemu-system-riscv32 --output symc-contract.json
uv run --script scripts/test-ws53.py --qemu ./qemu/build/qemu-system-riscv32
```

完整WS53套件包含两个SYMC入口。覆盖52组双通道双向固定向量、960组算法/模式/槽位/
半槽/长度/方向组合（含CTR进位）、18组故障、12组在途IO；另有82个非法配置、60个
描述符、30个密钥权限、12个运行中资源丢失、39个非法访问、10个公布顺序/所有权、
2个双引擎四通道共享IRQ及4个旧请求退休后复用场景。核对实际输出、IV、未修改输入/
描述符、保护字节和完成计数。

主工程`examples/ws53-symc-model`另运行生产SYMC/KM HAL的154个实际CPU场景：
120个算法/模式/方向/半槽组合与34个拒绝/超时/取消/丢弃场景。主机只写RAM命令邮箱，
CPU负责密钥装载和全部MMIO；主机检查结果、描述符、边界、访问计数与槽保留状态。
不安全的物理排空/内存归还接口未被调用，硬件仍可能在软件放弃后完成传输。

实板RAM/Flash/冷启动尚待验收。非整块流模式、CFB8/其他宽度、AEAD/MAC、更多环项/链式传输、PKE、
完整安全域/控制核服务和真实缓存/物理排空尚未实现。
