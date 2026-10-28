# WS53 指令缓存功能实验

此显式实验用于验证 Rust 指令缓存配置和维护代码的**指令可见性**。
它不以 CSR 保存写入值代替取指效果，也不是芯片微架构或时序的精确复刻。
默认和历史快照 profile 保持原有[拒绝边界](ws53-cache-state.md)。

仅适用于 QEMU v10.0.0 的 WS53 应用核、bare M-mode、单 hart、固定 Flash 映射：

```text
-M ws53,flashboot-handoff=on,cache-state-profile=instruction-experiment
-global ws53-wdt.assume-timing=on
```

缺少 Flash handoff 时启动拒绝；profile 不可在 QMP 初始化后切换。
控制核未模拟、未运行，模型不授予共享资源所有权或 DMA 安全能力。

## 来源与模型假设

SDK `163d9150ab94a02d09a05f623351f9facee3025e` 的 `arch/cache.h` 与
`ws53_liteos_app/libriscv.a` 中 `cache.c.obj` 支持以下编码事实：

- ICCTL `0x7c0`：bit0 开启，bits2:1 为 4/8/16/32 KiB；实际库拒绝 64 KiB。
- ICMAINT `0x7c2`：4 全体失效，5 按 VA 失效；ICINCVA `0x7c4` 为地址。
- APREFI `0x7c6`：SDK 写入 `((lines-1)<<1)|1`，lines 为 1–8。
- SDK 地址维护步长为 32 字节。库的这些函数没有维护完成位轮询。

以下是**有意选定的模型策略**，不是实测硬件事实：

| 项目 | 实验策略 |
| --- | --- |
| 初态 | ICCTL=6、APREFI=0、DCCTL=0、MEMATTRL=0x33333333、指令行为空；合成初态，非硅复位值 |
| 可缓存范围 | 0x400000–0x7fffff 的 Flash；ITCM/DTCM 等其他取指直通，不按 PMP/MEMATTR 推导缓存属性 |
| 行组织 | 32 字节，按尺寸提供 128/256/512/1024 行，直接映射替换；硬件关联度未证实 |
| 预取 | 开启时在需求行后顺序读取配置的 1–8 行，边界处截断；不是硬件预取算法证明 |
| 时机 | 在 TCG 翻译和前瞻取指时填充，缓存命中/填充计数不是退休指令数或周期 |
| 维护 | 同步完成并刷新 TCG TB；无忙位或延时，不证明真实完成/总线 drain |
| fence.i | 丢弃当前 hart 的全部模型指令行并刷新 TB；不清理 D-cache 或其他 master |
| 复位 | 清空模型指令行及本周期计数，保持调试改写后的 ROM backing；不是 Flash 恢复或冷启动 |

QEMU 原本会因 backing 代码改写自动使 TB 失效。这里额外保留独立的指令行快照，
使重新翻译仍能得到旧指令；ICMAINT/fence.i 再使执行得到新内容。
32/48位指令的所有半字、跨行/跨页取指、前瞻以及 semihosting 回看都经过可见性钩子。
原有地址权限检查和 TB 页面跟踪仍先运行。未绑定钩子的 WS63/BS2X CPU 不使用此模型。

## 操作与拒绝

ICCTL/APREFI 支持真实 CSR 读取、直接写和结果合法的 RMW。ICCTL 未知位、开启时
改尺寸、仍有有效行时改尺寸，以及 APREFI 未知位会在更新前拒绝。合法的启用/停用
保留指令行；停用期间取指使用 backing。尺寸变更须先停用并使所有行失效。

ICMAINT/ICINCVA 只接收 SDK 已证实的直接写（rd=x0）。读取、RMW、未知命令、
未设置有效地址就按 VA 失效、非32字节对齐或范围外地址均明确拒绝。
这些拒绝是模型范围限制，不表示硬件必然产生非法指令。数据缓存、其维护以及
MEMATTR 变化仍拒绝，不能由 DCCTL=0 推导无脏别名或已经排空。

## 测试工具与调试 backing

```bash
uv run --script scripts/ws53_instruction_cache_test.py --qemu ./qemu/build/qemu-system-riscv32
uv run --script scripts/test-ws53.py --qemu ./qemu/build/qemu-system-riscv32
```

取指测试使用第一方指令而非厂商固件。两轮各执行 8 阶段，每阶段包含32位函数、
另一独立行、16位压缩函数，以及跨页/跨缓存行的48位 l.li 函数。验证旧内容、新内容、
停用后直通、重新启用保留旧行、按 VA 仅更新目标行、fence.i 全体更新，以及模型复位。
每轮另执行10种不支持操作，检查精确 mcause/mepc/mtval、rd 保持及控制状态不变。
profile 运行中修改、无 Flash profile、运行中及越界调试写均须拒绝。

仅本实验公开 QOM `/machine` 的 `icache-debug-patch`：uint64 的高32位为对齐 Flash
地址，低32位为 backing 字。必须先通过 QMP stop 停止 CPU；该工具通过 QEMU 的
ROM 调试写路径修改 backing，不执行 SFC/真实 Flash 烧录，也不使模型指令行失效。
它用于制造可验证的旧/新指令差异，不能作为 hisiflash 或 SFC 写入测试。

只读诊断属性为 `icache-valid-lines`、`icache-translation-fetches`、
`icache-translation-hits`、`icache-line-fills`、`icache-prefetch-fills`、
`icache-invalidations` 和 `icache-fence-i`。均为本模型周期统计。
原 `cache-csr-rejections` 保留累计计数，epoch/tainted 遵循原状态模型约定。

开发验证另通过4种故障变体验证测试敏感性：去掉取指钩子、去掉 fence.i 钩子、
维护只更新计数器，以及 VA 操作错误地全体失效，分别在第2/4/3/7阶段因执行结果
不符被拒绝。恢复正常源码重建后二进制校验值与原始正常版本一致。

Rust 运行时接口的实际链接/执行集成、实板 RAM/Flash 启用、冷启动和精确定时仍待
单独验收。不能用此功能实验解释既有约1.58ms延后，或把其假设转为硬件参数。
