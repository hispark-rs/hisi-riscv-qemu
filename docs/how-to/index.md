# 操作指南 · 任务导向

照着做的操作配方，面向已经知道自己要完成什么任务的人。

- [安装与构建仿真器](install-and-build.md) —— 下载预编译 Release，或从源码构建。
- [运行固件](run-firmware.md) —— `run.sh` 用法、运行 C SDK app、用 `NV=1` 回填分区表/NV。
- [调试与追踪](debug-and-trace.md) —— GDB（`-s -S`）、trace 事件、`qemu.log`。
- [运行测试](run-tests.md) —— 寄存器级 qtest、C SDK fixture、Rust 冒烟。
- [扩展掩膜 ROM 桩](extend-rom-stubs.md) —— 确认并新增一个 ROM 函数仿真。
- [移植到新的 QEMU 版本](port-qemu-version.md) —— 为新 tag 补一套 patch-series。
