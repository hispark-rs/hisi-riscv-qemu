# WS63/BS21 TCXO 时间模型

状态寄存器实现 EN（bit2）、只读 VALID（bit4），清零及快照命令自清。
停止时冻结计数，重新使能续计；刷新后的两组 32 位或四组 16 位字来自同一个快照。
时间来源为 QEMU virtual clock，WS63 24 MHz、BS21 32 MHz；不再通过 MMIO 读次数
伪造时间推进。`muldiv64` 避免 nanosecond×frequency 的中间溢出。

直接 `-kernel` 模式继承原先运行中的时钟（初始 EN=1），这属于模型启动环境，
不宣称硅片 POR 默认使能。精确硬件快照延迟、功耗和睡眠恢复尚未建模。

回归：`uv run --script scripts/ws63_tcxo_test.py --qemu <binary>` 验证使能读回、
停钟、只读 VALID、快照一致性、每毫秒增量和 180 秒低 32 位回绕。
本次修复使基于 HAL 的显式时钟接入检查得到真实控制状态，不能在驱动侧绕过检查。
