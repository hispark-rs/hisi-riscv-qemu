# WS53 缓存 CSR 状态与拒绝范围

此模型只检查应用读取的 CSR 状态及对未支持操作的拒绝。它没有实现数据缓存、
指令缓存、脏行、别名、写缓冲排空或多主设备一致性，不能授予 DMA 内存安全资格。
模型返回非法指令异常是为了明确拒绝未实现行为，不代表真实芯片将这些操作视为非法。

## 明确选择已知状态

默认 `-M ws53` 使用 `cache-state-profile=unknown`：访问 `0x7c0..0x7ff`
均产生异常和具名 `guest_errors` 诊断，不再继承 WS63 的静默读零/写忽略。
未知的芯片复位值不填零。WS63 和 BS2X 仍保留其原有兼容模型。

只有显式选择下列配置，才能读取历史观测的三个值：

```text
-M ws53,cache-state-profile=observed-flash-baseline
```

| CSR | 名称 | 允许读取的值 |
| --- | --- | --- |
| `0x7c0` | ICCTL | `6` |
| `0x7c1` | DCCTL | `0` |
| `0x7d8` | MEMATTRL | `0x33333333` |

这些值来自历史 Flash 应用暂停后的只读快照，并非真实复位值或每次应用入口的证明。
配置独立于 `flashboot-handoff`；打开后者不会自动给缓存 CSR 建立已知值。
`cache-state-profile` 只可在机器初始化前设置。初始化后，包括同值设置在内的
QMP 修改均被拒绝，避免软件在一次实验中改变前提。

三个已知 CSR 只允许读取和结果完全不变的写入。CSRRS/CSRRC 及立即数形式按真实
RMW 结果检查，拒绝时既不更新 CSR，也不提交目标寄存器 rd。允许同值写只是
**模型状态保持不变的假设**，没有证明硬件写入的微架构副作用，更没有执行清理或排空。

| 操作 | 模型行为 |
| --- | --- |
| 更改 ICCTL/DCCTL，包括开启缓存、修改尺寸或未知位 | 异常及诊断，状态不变 |
| MEMATTRL 任一不同值；MEMATTRH 读写 | 异常及诊断；未观测 MEMATTRH 不补零 |
| ICMAINT/DCMAINT 读写，包括已知命令及零值 | 异常及诊断；没有完成位或忙状态假设 |
| ICINCVA/DCINCVA、APREFI/APREFD 读写 | 异常及诊断；没有地址掩码或预取状态假设 |
| 范围内其他 vendor CSR | 异常及诊断，不使用通用 RAZ/WI 后备 |

## 诊断、复位和 DMA 边界

只读 QOM 属性位于 `/machine`：

- `cache-csr-rejections`：进程内累计拒绝次数，`system_reset` 不清除。
- `cache-csr-epoch-rejections`：当前模型复位周期内拒绝次数。
- `cache-csr-tainted`：当前周期发生过至少一次拒绝。捕获异常后继续执行或同值写入
  都不会清除它；false 仅表示本周期未拒绝，不是缓存或 DMA 的资格判断。

`system_reset` 重建所选实验的初始状态并清除本周期计数，不模拟芯片复位清缓存，
也不对未观测的状态作推断。本模型没有调用 MDMA 的数据传输、pending-write 清除、
时钟或复位接口；读取 DCCTL=0 不会自动开启 MDMA 的 `assume-profile`。
MEMATTR nibble 3 在 SDK 中是 **non-cacheable、bufferable**，不能由此推导物理 drain、
没有脏别名或其他主设备已交出 RAM。

## 证据来源与限制

来源 SDK 提交：`163d9150ab94a02d09a05f623351f9facee3025e`。

- `src/kernel/liteos/liteos_v208.5.0/Huawei_LiteOS/arch/riscv/include/arch/cache.h:63`
  定义 ICCTL/DCCTL enable bit0、尺寸 bits2:1、维护/VA/prefetch CSR；头文件的 default
  注释不作为本模型的芯片复位证据。
- `src/drivers/chips/ws53/include/arch_encoding.h:192` 定义 MEMATTRL/H 地址。
- `src/include/driver/drv_pmp.h:40` 与
  `src/drivers/drivers/hal/pmp/v31/hal_pmp_riscv31_regs_op.c:52`
  定义属性及每域四位打包；PMP 禁用时的属性选择、锁定影响并未在此模型实现。
- `src/kernel/liteos/liteos_v208.5.0/ws53_liteos_app/libriscv.a` 中 `cache.c.obj`
  确认 enable 只接受尺寸代码 0/2/4/6，写 size|1；维护使用 `0x7c3` 的
  4/12 与按 VA 的 5/9/13，地址写入 `0x7c5`，SDK 步长 32 字节。
  选定对象中的 CSR 操作均为写入，没有完成位读回轮询。后续 fence 不能证明 DMA drain。

源身份：cache.h SHA-256 `bf6be3e822ac6ce99619462157c38c9089659d79e48ced848fbf689c795b06e2`；
libriscv.a `4152f64d9aea2cac0dc7cbf04df10cd1e8d0c6525a5d50ea7efca5ed7c8bf019`；
cache.c.obj `809e8fb9e9d832f3f0093bc36ddd01fb2b45d321adfd1cb081f4bca2c6e423d1`。
模型和测试不需要也不运行这些厂商二进制。

## 自动验证

```bash
uv run --script scripts/ws53_cache_state_test.py --qemu ./qemu/build/qemu-system-riscv32
```

测试用第一方 RISC-V 指令在真实 TCG 上执行两个 WS53 profile：读取全部 64 个 CSR，
并对具名 CSR 执行直接写、立即数和原子 RMW。异常必须给出正确 mcause、mepc、mtval；rd、已知
CSR 和内存哨兵保持不变。每个配置重复两轮并检查复位、QMP 拒绝和精确诊断计数。
另在 WS63、BS20、BS21、BS21E、BS22 上检查原 RAZ/WI 行为未变。
该测试也由 `scripts/test-ws53.py` 调用。它不能替代实板缓存、Flash 冷启动或 DMA 验收。

## 旧 ROM 拦截的机器边界

旧 BS21 ROM 兼容范围 `0x10000..0x40000` 与 WS53 ITCM 重叠。v10 补丁 0010
在 CPU 实例中保存 `hisi_legacy_rom_disabled`；WS53 在 realize 前设置它，CPU reset
不清除该配置。WS63 与 BS21 两条 ROM 拦截都检查该开关，避免 WS53 的真实非法
指令异常被未知 ROM 函数的成功桩吞掉。旧机器的默认行为不变。

```bash
uv run --script scripts/ws53_rom_trap_test.py --qemu ./qemu/build/qemu-system-riscv32
```

这个 TCG 回归在 WS53 ITCM 低端、中部和高端执行普通非法编码和被拒绝的 CSR，
逐条检查 cause2、mepc、mtval、mret 返回及 a0/ra/内存哨兵；每处重复两次系统复位。
WS53 没有映射旧 WS63 ROM 范围，因此第二条 hook 的 WS53 排除以源码检查为依据，
不把未映射地址的取指异常冒充 cause2 实测。五个旧机器另执行当前模型地址对应的
memset、memcpy 与越界拒绝，检查返回值和内存边界。这些只验证已有 ROM 兼容桩，
不证明五种真实芯片具有相同 ROM ABI。旧 shell 冒烟脚本的 memset 地址也同步为
当前模型的 `0x3d1dc`。完整 `scripts/test-ws53.py` 同时运行上述两个新测试。
