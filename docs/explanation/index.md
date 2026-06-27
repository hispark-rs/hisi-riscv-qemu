# 解释 · 理解导向

背景与权衡的讨论——理解某件事**为什么**这样设计、边界在哪。

- [设计取舍：为什么 fork QEMU](design-rationale.md) —— 方法、为何不用 `-M virt`、按版本维护 patch-series。
- [中断控制器设计](interrupt-controller.md) —— 两类中断线（mie 类与自定义本地类）的投递机制。
- [已知边界与非目标](limitations.md) —— ROM 数据墙、语义边界、已知简化、冻结项。
- [多角度对齐分析](alignment-analysis.md) —— 用 QEMU 交叉验证 rs HAL ↔ C SDK ↔ SVD。
- [Rust 工具链是否需要 xlinx](rust-toolchain-xlinx.md) —— 调研结论与建议。
- [BS21 连接性仿真可行性](bs21-connectivity-feasibility.md) —— BLE/SLE 边界为何是死胡同。
- [BS21 厂商固件运行现状](bs21-vendor-firmware.md) —— `-M bs21` 上的签名镜像格式与边界。
