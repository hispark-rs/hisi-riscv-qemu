# 移植到新的 QEMU 版本

WS63 模型是上游 QEMU 的树外 overlay：**新增文件**（`hw/riscv/ws63.c`、`trans_xlinx.c.inc`、`ws63-test.c`）由
`build.sh` 拷入；**对既有 QEMU 文件的改动**走按版本分目录的 `git format-patch` 序列 `patches/<tag>/`。

这些改动会随 QEMU 版本漂移（头文件搬家、结构体/字段偏移变化、惯用法变更），所以序列**按版本维护**。本指南讲怎么把序列
移植到一个新的 QEMU tag。序列的结构与各补丁职责见 [patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)。

## 步骤

1. **确认未移植**：

   ```bash
   QEMU_TAG=<new> QEMU_DIR=/tmp/q bash scripts/build.sh
   ```

   若 `patches/<new>/` 不存在，`build.sh` 会失败并列出已支持版本。

2. **套用最近版本的序列并解决冲突**：克隆该 tag，拷入 `src/` 文件，用 `git apply --reject`（或 GNU
   `patch --fuzz`）套用最近版本的序列，解决 `.rej`，修掉 API 漂移，直到能构建且 `scripts/qtest.sh` 通过。

3. **重新生成序列**：把改动按 `0001`/`0002`/`0003` 的同样分组提交，`git format-patch`，把结果放进
   `patches/<new>/`（若拷入的 `ws63.c` 需要适配旧/新 API，再加一个 `0004` 兼容补丁）。

4. **接入 CI**：把版本加进 `qtest-matrix.yml`；如需把它设为默认基线，调整 `QEMU_TAG` 的默认值。

## 漂移有多大（参考）

同一套支持在不同版本间的漂移很说明问题：

- **10.0 → 10.2**：`insn_len` 移到 `internals.h`、CPU 定义改声明式 `DEFINE_RISCV_CPU`、`decode_opc` 改表驱动、
  `CharBackend`→`CharFrontend`、`exec/`→`system/address-spaces.h`。
- **10.2 → 11**：六个 `hw/*.h`→`hw/core/*.h` 头大迁移。

——正因如此才按版本分目录维护。各版本 `0004`/`0005` 补丁的具体差异见 [patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)。

## 相关

- 序列结构与各补丁职责 → [patch-series 参考](https://github.com/hispark-rs/hisi-riscv-qemu/blob/master/patches/README.md)
- 改 ROM 桩后如何落回 `0001` → [扩展掩膜 ROM 桩](extend-rom-stubs.md)
- 为什么用 fork + patch-series 而非树外插件 → [设计取舍](../explanation/design-rationale.md)
