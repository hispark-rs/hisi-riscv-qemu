# 扩展掩膜 ROM 桩

当一个固件调用了尚未仿真的掩膜 ROM 函数时，它会走 `default` 分支返回 0（成功）——多数情况无碍，但有时你需要
**真正仿真**某个 ROM 函数。本指南讲怎么定位、确认、新增一个 ROM 桩。

背景（为什么需要这些桩、机制如何）见 [ROM 桩目录参考](../reference/rom-stubs.md) 与 [已知边界](../explanation/limitations.md)。

## 找一个新的 ROM 函数地址

ROM 函数地址落在 `0x109000..0x14C000` 区间。从 SDK 的符号表查：

```bash
grep '<name>' fbb_ws63/src/drivers/chips/ws63/rom_config/acore/acore.sym
```

地址落在该区间即为 ROM 函数，运行时会走 `ws63_rom_call`。

## 看哪些 ROM 地址被实际调到

在 `ws63_rom_call` 顶部临时加一行打印：

```c
fprintf(stderr, "ROM pc=0x%x\n", pc);
```

> 注意 `*printf_s` 族很吵，按地址区间过滤再看。

## 新增一个仿真

在 `ws63_rom_call`（位于 target/riscv 补丁的 `cpu_helper.c`）的 `switch` 里按地址加 `case`，从 `a0..a7`
（`gpr[10..17]`）取参，设返回值 `ret`：

```c
case 0x10aeb4:  /* memcpy_s(dst, dmax, src, n) */
    /* 从 gpr[10..13] 取参，写内存，设 ret */
    break;
```

返回约定：`a0(gpr[10]) = ret;pc = ra(gpr[1])`，即直接返回到调用者。

## 把改动落回 patch-series

改完 `target/riscv/` 后，需要重新生成 patch-series 的 `0001`：

1. 在 `qemu/` 树里把改动提交。
2. `git format-patch` 出对应补丁，覆盖 `patches/<QEMU_TAG>/0001-target-riscv-*.patch`
   （或最简单：`git -C qemu diff -- target/riscv/ hw/riscv/meson.build ...` 后手工归入对应 `000N` 补丁）。
3. `scripts/build.sh` 重新构建验证。

> **务必覆盖整个 `target/riscv/` 改动**——漏掉如 `translate.c`（xlinx 解码 hook）的 hunk，全新克隆套用残缺补丁后
> 无法解码 xlinx、C SDK 固件零输出。生成后用 `git apply --check` 对 pristine 基线验证整条序列可干净套用。

多版本下补丁怎么维护，见 [移植到新的 QEMU 版本](port-qemu-version.md)。

## 相关

- 已仿真的 ROM 函数与设备桩清单 → [ROM 桩目录](../reference/rom-stubs.md)
- 为什么有些东西桩不了（ROM 数据墙）→ [已知边界与非目标](../explanation/limitations.md)
