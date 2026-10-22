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
clock-management/low-power behavior is not yet modeled. UART uses a WS53-specific
word-access model instead of the legacy family's different receive flags.
Full UART TX FIFO/timing/flow-control/DMA validation is outstanding. None of these results prove
silicon timing, electrical properties or control-core coexistence.

The SDK common-register C structure and its comments disagree by 0x10. Neither
common window is modeled: reads/writes are diagnosed as unimplemented. Tests use
local raw/masked status at channel +0x18/+0x1c. The SDK writes one to channel EOI,
but that does not establish read behavior; EOI reads also report unimplemented.
All normal tests must run without these diagnostics. A separate negative test
requires diagnostics for 11 unsupported reads and 3 unsupported writes, so an
unknown register returning zero cannot silently satisfy a test. Zero state after
model reset is deterministic simulation initialization, not a silicon reset claim.

The UART model owns a 64-byte RX FIFO for each of the three instances. It models
FIFO order, drop-new-data FIFO overrun, overwrite-on-overrun non-FIFO mode,
parity/framing test injection, read-clear line
errors, RX thresholds, IRQ masks and pending delivery, FIFO reset and system
reset. SDK FIFO access mode (`FAR` / `RX_FIFO_WRITE`) supplies controlled data and
error inputs; DATA TX also supports loopback. `FIFO_STATUS` bit2 means not-empty
and bit3 full; LINE_STATUS data-available is bit5. The RAM test's optional
`--elf-uart-rx` image (build with `uart-rx-smoke`) consumes ten actual chardev
bytes through the Rust HAL and checks that the FIFO then reports empty.

The SDK does not establish the character-timeout duration. The model's
`rx-timeout-ns` property defaults to zero (no synthesized timeout); tests inject
100000 ns explicitly to exercise IRQ ID12 and cancellation. This is a test input,
not a WS53 timing constant. Error-before-data IRQ priority is also a model policy
awaiting hardware verification. UART reset supplies an explicitly configured
console handoff, not measured silicon resets. Register width follows SDK word
accesses; rejection of halfword accesses is the model contract, not a claim
that silicon rejects them.

TX currently goes straight to the chardev: no TX FIFO, shift-register delay or
baud-accurate serial line is claimed. Modem/TX IRQs, auto flow control, PRO fields,
DMA and unsupported MMIO are diagnosed rather than absorbed. FIFO-mode changes
without an explicit RX reset are also unmodeled. Preserved configuration values
alone are not proof of framing/divider hardware behavior. The experimental HAL
now has fallible receive APIs, but full clock/pin configuration and physical
validation remain open; it must not be marked a complete UART driver.

Optional `--elf-uart-errors` runs the RAM example built with `uart-error-smoke`.
It tests the actual Rust HAL on all three UARTs: simultaneous error flags,
corrupt-head discard, preservation of queued bytes on overrun, a good prefix
before an errored byte, deferred error delivery after a partial read, zero-length
read preservation and recovery. This image deliberately resets RX FIFOs and
injects errors through FAR; it is not the default board image. `--elf-uart-combined`
expects both UART test features and additionally injects ten chardev bytes.
Break decoding has host coverage only; no break-injection hardware proof is claimed.

`--elf-uart-irq` runs the `uart-irq-smoke` RAM variant. Each of IRQs 39, 41 and
42 enters the real Rust runtime/handler six times, testing queued data on unmask,
data arriving after a one-shot notification, deferred parity/framing and overrun
errors, injected character timeout, cancellation and recovery. The ISR captures
IIR, masks RX/error notifications and disables/acknowledges the local IRQ. It
does not read FIFO data or read-clear LSR, so the foreground HAL retains the
error report. Rearming requires both peripheral and controller enable; an empty
FIFO must not retrigger. The runner explicitly injects a 100000 ns timeout for
this variant. This tests deferral, not hardware timeout timing or async support.
`--elf-uart-all` expects all three test features and exercises them together;
`--elf-uart-combined` retains the original RX-plus-errors two-feature meaning.

The machine also supplies three independent `ws53-gpio` basic banks at
0x5700c000/0x57028000/0x57010000, with 23/12/3 bit spans and IRQs 34/33/29.
`scripts/ws53_gpio_test.py` (also called by `test-ws53.py`) checks input versus
output-latch reads, word SET/CLR commands, output and drive-enable wires,
rising/falling/dual edges, high/low levels, masking, selective EOI, isolation
and system reset. Tests drive named `pin` inputs and observe `out` and
`output-enable` signals: behavior is not satisfied by register storage alone.

GPIO model policies are explicit: edges latch only on enabled input/edge pins;
masking retains a captured edge; disabling hides it without acknowledging it;
EOI clears edge latches but cannot suppress an active level. Configuration
changes alone do not fabricate an edge. Deterministic reset disables/masks
interrupts and clears output latches while preserving external input levels.
These are simulation contracts awaiting physical validation. The 23-bit bank
is not a claim of 23 independently bonded pads: pad/SDK aliases, mux routing,
SWD reservation, shared clocks and analog electrical effects are not integrated.

SW_CTL's data-source encoding, LOCK/LP registers, MULTISYSTEM extensions and
debounce timing are not established. Reads/writes of those unsupported controls,
command reads and RO-status writes must report diagnostics; enabling debounce
also reports unimplemented rather than silently pretending to filter input.
Fifteen negative accesses/signals enforce this. Only debounce-disabled operation
is exercised. No GPIO HAL or Rust GPIO ISR support is implied by these qtests.

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
