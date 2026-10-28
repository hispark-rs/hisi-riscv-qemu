# WS53 厂商应用入口交接模型

`-M ws53,flashboot-handoff=on -global ws53-wdt.assume-timing=on` 为实验性测试
profile，默认关闭。依据 SDK `163d9150` 的 `dyn_mem.c`、内存配置及 A 板
2026-10-24 `flash-entry-observe-04` 实际入口观察。它从应用 ELF 的 `0x45d300`
开始执行；没有模拟 BootROM、厂商 FlashBoot、签名校验、串口下载、Flash 擦写
或控制核固件，也不能作为冷启动证据。

该属性只能在机器初始化前设置。初始化后通过 QMP `qom-set` 写入任意值
（包括重复当前值）都会拒绝；不会改变映射、RAM 内容或复位配置。切换测试
profile 应启动另一个 QEMU 实例，不能把它用作运行时复位恢复接口。

UART2专用Flash应用可另用 `-global ws53-pinctrl.assume-uart-tx=on` 和
`-global ws53-pinctrl.uart-l0-console-profile=on`，仅预置AGPIO1/2的mode1，
MGPIO17/18保持GPIO。此设置来自限定L0交接假设，不是硅片复位值。
既有`uart-console-profile=on`仍同时预置H0的MGPIO17/18 mode2及L0；
两种profile互斥，缺少TX模型亦拒绝。`system_reset`恢复各自明确选择的模型配置。

- Flash 只读映射 `0x400000..0x800000`；ELF 文件内容按 LMA 装载。
- 每次模型复位以 `0xa5` 填充应用 ITCM/DTCM，测试前必须读回确认；由 guest
  执行 data 复制和 bss 清零。此初值不是实际 SRAM 复位值。
- 初始 `BRAM_SHARE=0x3f0`、`DTCM_SHARE=0x20`、MCU模式0、控制核复位未释放。
  实现 `0x20→0x3c` 模式1路径；模式外配置会诊断并拒绝。
- DTCM RAM2–5 各32KiB按接口选择使能；RAM6/7、8/9、10/11各64KiB按各自
  门控和归属生效。访问关闭或交给控制核的块产生总线错误并输出诊断；重新开启
  保留内容。ITCM栈所在RAM0/1保持可访问。其余ITCM别名、RAM12/13及控制核
  地址空间没有建模。切换归属时相应门控必须先关闭。
- `M_CLKEN1=0xffff`、WDT控制`0x3d`取自入口观察；7秒看门狗起点来自SDK。
  低字节、精确倒数时序仍使用已有显式假设，必须开启`assume-timing`；到期可
  验证请求，完整SoC复位拓扑仍未实现。UART/TIMER继承现有配置模型。

```sh
uv run --script scripts/ws53_flash_handoff_test.py --qemu /path/to/qemu-system-riscv32
uv run --script scripts/test-ws53.py --qemu /path/to/qemu-system-riscv32
```

前者同时验证两种启动配置在初始化后拒绝属性修改，并覆盖内存切换、门控期间读写失败、内容保持、非法归属切换拒绝、只读复位
状态、复位恢复和继承看门狗到期。后者同时保留原有默认 RAM 外设回归。
主工程 `scripts/test-ws53-flash-qemu.py` 则执行实际 Rust Flash ELF 的两轮交接。

`ws53-pinctrl.pad-trace` 可绑定字符后端以记录所有已建模pad的数字状态变化：

```sh
-chardev file,id=padtrace,path=/tmp/pads.csv -global ws53-pinctrl.pad-trace=padtrace
```

CSV列：虚拟纳秒时间、SDK pin编号、level、driving、floating、contention。
每次复位重新记录初态；同一状态不重复输出。该功能不改变引脚解析规则，不把
浮空值当成有效驱动，也没有模拟模拟电压、负载或传输线。主工程检查 MIO17/D5
32次10ms翻转，将原始trace及其哈希保存，等待与真实DSLogic波形逐项对照。
