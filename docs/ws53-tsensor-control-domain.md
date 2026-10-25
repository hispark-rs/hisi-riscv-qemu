# WS53 Flash 温度模块的控制子系统复位门禁

这项变更把 `-M ws53,flashboot-handoff=on` 的控制域复位状态接入 TSENSOR。
控制核 CPU 仍未建模、未启动。默认 RAM takeover 模式继续使用既有的“域已释放”
前提，不据此宣称所有 RAM 上电状态都相同。

## 来源和范围

SDK 固定提交 `163d9150ab94a02d09a05f623351f9facee3025e`：

- `drivers/chips/ws53/include/acore/platform_core.h:107-110` 定义
  `HAL_SOFT_RST_2_GLB_B=0x5703096c`、`RESET_CRG_CCORE_VALUE=3`、
  `RESET_CCORE_VALUE=7`。
- `application/ws53/ws53_application/main.c:300-310` 写3释放逻辑/CRG，
  等待5us，之后写7才释放CPU。模型只接受32位写0/3，其他操作报告未实现且不改变
  状态。这是模型限定，不是在断言硬件不允许其他编码或访问宽度。
- `drivers/chips/ws53/include/clock_recover.h:101,165-181` 描述
  `0x59000810` 的温度APB和1MHz时钟字段。本次没有实现整个控制域时钟寄存器。

2026-10-25 A板的独立实板诊断 `tsensor10-domain-probe-01/probe.log`：
状态0时 CONTROL/STATUS/CKEN0 均读到异常相同值；只写3后，两遍均读回
CONTROL=STATUS=0、CKEN0=0xfeff。撤回到0之后访问温度寄存器导致调试总线busy，
需要单独恢复。这证明“CPU未启动”和“其外设逻辑/时钟已释放”不是同一条件。

模型不会返回该异常字样常量，也不模拟调试总线挂起。域处于0时，温度所有MMIO
访问返回 QEMU `MEMTX_ERROR` 并记录 `inaccessible control domain`。
这是一条让软件明确失败的仿真政策；实际芯片是否陷入CPU访问异常、总线等待或返回
不确定数据，并未由模型证明。Qtest协议本身会丢弃 MemTxResult，不能把其返回的零
当作可用寄存器；测试另外使用真实TCG指令验证 load/store access fault。

## 行为

- Flash机器复位保持 `0x5703096c=0`，禁止温度MMIO。
- 写3使温度MMIO可用，但不初始化/启动转换，不释放控制CPU。
- 重复写3不清除已有READY，不重启转换。
- 写0取消转换定时器和部分累加，清除CONTROL/READY/配置；再次写3需要重新初始化。
- 系统复位恢复状态0。设备自身复位不会把机器持有的域状态偷偷改为已释放。
- 外部输入码和已有故障输入按既有契约保留，不能靠域复位消除外部clock fault。
- 5us硬件等待、上游电源、APB/采样门控、跨核协作及debug busy恢复不在本模型中。

## 测试

在独立的已构建QEMU上运行，无需SDK、串口或实板：

```sh
uv run --script scripts/ws53_tsensor_domain_test.py \
  --qemu /path/to/qemu-system-riscv32 --output /path/to/new-results
```

测试包含5次域保持时读、5次被拒绝配置写、8次未支持的reset写、3轮系统复位，
正常转换、重复释放、半转换取消、外部故障保持，以及真实CPU的三次访问异常
（cause5/7/5和精确mepc/mtval）。还运行原温度1024个输入码和故障/复位回归、
原Flash内存/启动属性/看门狗回归。

冻结候选10原ELF还可单独做负向诊断，SHA必须为
`8950d0ee0597ef31ff89caee7279b920f28017c77063f84c647b6af9fabeb410`：

```sh
uv run --script scripts/ws53_tsensor_domain_candidate10_test.py \
  --qemu /path/to/qemu-system-riscv32 --elf /path/to/frozen-candidate10.elf \
  --output /path/to/new-negative-results
```

需要本机 `llvm-nm`、`riscv64-linux-gnu-addr2line` 和
`riscv64-linux-gnu-objdump`，只读取ELF。结果明确标为独立负向诊断，
`candidate10_qualified=false`，不会替代旧候选资格或HIL结果。

实跑结果：原候选10的UART完成TCXO、PWM和两路RTC后，首次CONTROL读取在
`mepc=0x00461442`、`mtval=0x59006304` 引发cause5，源码为
`examples/ws53-flash/src/tsensor_flash.rs:98`。
CPU停在 `default_exc_handler=0x0045e2e0` 的 `c.j .` 自环；继续运行后仍无温度
BEGIN或最终PASS，STATE/WINDOW/SAMPLE和转换启动计数均为零。
此故障与实板code100不是同一种错误编码，两者均指向未建立可用寄存器访问前提。

没有改变冻结候选10、旧QEMU、旧资格脚本或主工作树。本提交不包含厂商备份、
Flash内容、Rust ELF或者新的Flash稳定性声明。后续新HAL显式域交接必须使用新
候选和新的QEMU/Flash/冷启动证据。
