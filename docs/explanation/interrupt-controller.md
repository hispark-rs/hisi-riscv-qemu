# 中断控制器设计

本文讨论 WS63 中断控制器（`ws63-intc`）的设计与投递机制**为什么这样**。CSR 地址、IRQ 号等事实清单见
[机器模型 §中断控制器](../reference/machine-model.md#中断控制器ws63-intc)。

WS63 用 HiSilicon 自定义的「riscv31」核内 CLIC 式方案，不是 CLINT/PLIC。设备 IRQ 分两类，这两类的差异是整个
设计的关键。

## 两类中断线

### IRQ 26–31（mie 类）：TIMER_0/1/2、RTC、I2C0

这些用**标准 `mie` 位**。固件经真实 `mie` CSR 使能；`ws63-intc` 收到外设 IRQ 线后用
`riscv_cpu_update_mip(env, 1<<n)` 拉高 `mip[n]`，QEMU 经**向量化 mtvec**（mode 1）派发到 `mtvec + 4*n`。
**完整保真、已实测**（见 `timer_irq` 示例）。

这一类能直接复用 QEMU 既有的 RISC-V 中断机制，因为 IRQ 号 < 32，放得进 RV32 的 32 位 `mip`/`mie`。

### IRQ ≥32（自定义本地类）：GPIO=33、UART=53…LSADC=72

这一类是**为什么需要 target/riscv 补丁**的根本原因。它们用核内自定义 CSR `LOCIEN0-2`（0xBE0）/
`LOCIPD0-2`（0xBE8）/`LOCIPCLR`（0xBF0），且 mcause 取值 32–72 **放不进 RV32 的 32 位 mip/mie**——所以
QEMU 原生机制无法表达它们。

通过 `target/riscv` 补丁（patch-series `0001`）核内投递：

- `CPUArchState` 加 `ws63_locien/locipd` 状态。
- `riscv_cpu_local_irq_pending()` 在标准 mip/mie 检查之后增查 `locipd & locien`（受 `mstatus.MIE` 门控），
  返回 IRQ 号。
- `riscv_cpu_do_interrupt()` 既有逻辑即以 `mcause = irq` 投递（向量化时 `mtvec + 4*irq`）。
- 设备经新导出的 `riscv_cpu_set_local_irq(env, irq, level)` 置/清 `locipd`；`LOCIEN`/`LOCIPCLR` CSR 写更新
  enable/清 pending。

**完整投递、已实测**（见 `gpio_irq` 示例，GPIO0 pin0→IRQ 33）。这条自定义 pending 通路绕开了 RV32 32 位
mip/mie，对应 ws63-rt 的 `local_interrupt_handler`。

## 优先级 / 阈值

`ws63_local_irq_pending()` 读 `LOCIPRI`（每 IRQ 4 位，8 个/寄存器）取优先级，仅当
**优先级 > PRITHD（严格大于）** 才投递；多个候选取最高优先级、同级取最小 IRQ 号。复位默认每 IRQ 优先级 1、
阈值 0（即退化为按号投递，兼容旧行为）。CSR 写经 `ws63_loci_write` 镜像入 `env->ws63_locipri[]/ws63_prithd`。
已用 LOCIPRI/PRITHD 探针实测（屏蔽 / 抢占 / 严格 `>` 边界 5/5 通过）。

投递时**自动清 LOCIPD**（边沿/一次性，匹配 C SDK 的 `default_local_interrupt_handler` 无 LOCIPCLR 通路）。

## 已知简化

被阈值屏蔽的「已挂起」IRQ 在**仅写 CSR 降低阈值**（无新边沿）时不会自动重投——需要一次新的中断源边沿。
固件一般「先配优先级/阈值、后开中断源」故不触及；已实测该常规顺序 5/5 通过。

## 小结

mie 类（26–31）与自定义本地类（≥32）两条中断线现均已端到端验证。前者复用 QEMU 原生机制，后者必须靠
target/riscv 补丁才能表达——这正是项目需要 fork QEMU 而非树外插件的具体例证之一（见 [设计取舍](design-rationale.md)）。

## 相关

- CSR 地址与 IRQ 号清单 → [机器模型](../reference/machine-model.md#中断控制器ws63-intc)
- 为什么 fork QEMU → [设计取舍](design-rationale.md)
- patch-series 中 `0001` 的职责 → [patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)
