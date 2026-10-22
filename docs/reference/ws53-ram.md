# WS53 RAM test machine

`-M ws53` currently models the application-core MCU_MODE_1 RAM takeover
environment, not a flash-bootable product or the vendor control-core firmware.
Supported build pin: QEMU v10.0.0 (`7c949c53e936aa3a658d84ab53bae5cadaa5d59c`).

Mapped regions: acore ITCM `0x20000..0x30000`, DTCM
`0x20010000..0x20060000`, three basic UARTs, two 32 MHz timers, local interrupt
controller/custom CSRs. There are no catch-all MMIO absorbers, flash/ROM stubs,
shared-memory aliases or fake control-core responses.

The 32 MHz rate follows the pinned SDK application `dev_work_clock_init()`;
it overwrites the menuconfig's initial 30 MHz value. This is a boot-profile
assumption, not an oscillator measurement on the attached board.

The timer model supports the first milestone's 32-bit path: one-shot/periodic,
counter snapshot, mask/unmask, per-channel W1C, stop and reset. SDK profile
`CONFIG_TIMER_0_WIDTH_64=0` and `CONFIG_TIMER_1_WIDTH_64=0`. High-half/permission/
clock-management/low-power behavior is not yet modeled. UART uses the existing
basic family model; UART2 TX is covered by the RAM integration test. Full UART
FIFO/error/timing/reset validation is outstanding. None of these results prove
silicon timing, electrical properties or control-core coexistence.

The SDK common-register C structure and its comments disagree by 0x10. Neither
common window is modeled: reads/writes are diagnosed as unimplemented. Tests use
local raw/masked status at channel +0x18/+0x1c. The SDK writes one to channel EOI,
but that does not establish read behavior; EOI reads also report unimplemented.
All normal tests must run without these diagnostics. A separate negative test
requires diagnostics for 11 unsupported reads and 3 unsupported writes, so an
unknown register returning zero cannot silently satisfy a test. Zero state after
model reset is deterministic simulation initialization, not a silicon reset claim.

Build with `scripts/build.sh` using a private QEMU source tree. Then:

```sh
uv run scripts/test-ws53.py --qemu /path/to/qemu-system-riscv32 \
  --elf /path/to/ws53_ram_smoke
QTEST_QEMU_BINARY=/path/to/qemu-system-riscv32 /path/to/build/tests/qtest/ws63-test
```

The test checks 9 named timer behaviors plus the unsupported-access diagnostics.
With the RAM ELF it also checks data/BSS, UART2 output and three IRQ 53 entries
and returns through the guest runtime. All are simulation tests; the 20-round
physical-board RAM/IRQ acceptance remains outstanding.

Build host notes: this host's system Python lacked distlib; a uv Python 3.12
virtualenv with distlib, meson 1.5.0, ninja and pycotap was passed to QEMU
`configure --python=...`. GitLab dtc fetch failed; the exact same pinned commit
was obtained from QEMU's official GitHub mirror. The new host compiler reports
additional warnings in QEMU 10 and dtc: `meson configure -Dc_args=-Wno-error`
was used for this local build. These are build-environment adjustments, not
changes to guest hardware behavior. They do not suppress actual compile errors.
