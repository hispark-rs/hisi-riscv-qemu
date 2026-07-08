# 运行测试

ws63-qemu 有三层测试，互补：**寄存器级 qtest**（免启动、毫秒级）、**C SDK 外设样例**（仓库内预构建厂商固件）、
**ws63-rs Rust 冒烟**（进阶/CI 覆盖）。本指南讲怎么各自跑起来。每层验证了什么、覆盖到哪，见
[验证覆盖范围参考](../reference/verification.md)。

## 寄存器级 qtest（免启动回归）

`scripts/qtest.sh` 用 libqtest **直接驱动外设寄存器**（测试进程扮演 CPU 角色，**不启动固件**），毫秒级验证
GPIO/UART/timer/INTC/DMA 模型的寄存器语义。

```bash
bash scripts/qtest.sh          # 构建 tests/qtest/ws63-test 并运行（4 例,TAP 输出）
```

覆盖：GPIO 数据 set/clr/OEN/INT-EN 读写；UART FIFO/行状态复位值；timer 装载/使能/触发 + 经 INTC 投递
IRQ 26（`qtest_irq_intercept_in`）；DMA 通道 0 mem→mem 搬运 + 完成位。

## C SDK 外设样例

`scripts/csdk-test.sh` 启动 `tests/csdk/` 里**预编译的 fbb_ws63 C SDK 厂商固件**并断言各自的 UART 成功标志——
用**真实厂商固件**交叉验证外设模型。纯净 + 免 SDK/工具链（用已提交的 fixture）：

```bash
bash scripts/csdk-test.sh      # 当前 5/5 绿
```

样例列表、成功标志、以及两个「已记录但未断言」样例（timer / watchdog）的诊断，见
[验证覆盖范围 §C SDK 外设样例](../reference/verification.md#c-sdk-外设样例)。

> 重新生成 fixture：`scripts/build-csdk-samples.sh`（从 fbb_ws63 checkout 选一个 `CONFIG_SAMPLE_SUPPORT_*`、
> 干净构建、strip 到 ~400 KB）。

## ws63-rs（Rust）冒烟

`scripts/smoke-test.sh` 启动 ws63-rs Rust 固件，端到端断言串口/MMIO 标志。这是开发和 CI 验证路径，不是入门
happy path；运行前需要先 checkout 并构建 `hisi-riscv-rs` 示例固件。

```bash
WS63_RS=../ws63-rs bash scripts/smoke-test.sh
```

固件列表与成功判据见 [验证覆盖范围 §ws63-rs 冒烟](../reference/verification.md#ws63-rsrust冒烟)。

## 在 CI 里跑的是什么

`ci.yml` 每次 push/PR 跑上述全部三层（Ubuntu）；`qtest-matrix.yml` 在每个有 `patches/<tag>/` 的 QEMU 版本上跑
qtest。门禁清单见 [验证覆盖范围 §持续验证](../reference/verification.md#持续验证ci)。

## QEMU 官方文档

- [QTest Device Emulation Testing Framework](https://www.qemu.org/docs/master/devel/testing/qtest.html) —— libqtest 的上游说明（`scripts/qtest.sh` 即基于它）

## 相关

- 每项测试验证了什么 → [验证覆盖范围](../reference/verification.md)
- 把测试扩展到新 QEMU 版本 → [移植到新的 QEMU 版本](port-qemu-version.md)
