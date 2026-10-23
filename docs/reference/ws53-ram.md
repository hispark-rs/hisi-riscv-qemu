# WS53 RAM test machine

`-M ws53` currently models the application-core MCU_MODE_1 RAM takeover
environment, not a flash-bootable product or the vendor control-core firmware.
Supported build pin: QEMU v10.0.0 (`7c949c53e936aa3a658d84ab53bae5cadaa5d59c`).

Mapped regions: acore ITCM `0x20000..0x30000`, DTCM
`0x20010000..0x20060000`, three basic UARTs, two 32 MHz timers, three GPIO banks,
MCU/AON/TZPC pad controls, NMI aggregation and local interrupt controller/custom CSRs.
There are no catch-all MMIO absorbers, flash/ROM stubs,
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
is exercised. These qtests alone are not Rust GPIO ISR proof.

The default machine connects the three GPIO banks through `ws53-pinctrl`.
The SDK-derived fixture covers 31 canonical pads and 30 GPIO routes across MCU,
AON and TZPC register domains. Mode 0 routes GPIO; input-enable gates sampling,
pulls resolve undriven inputs, and an external driver overrides a weak pull.
The model exposes level, drive-enable, floating and contention signals. A
conflict deterministically resolves to the GPIO output and floating resolves
low; neither rule predicts an analog voltage. Drive and Schmitt codes are
retained without modeling current or hysteresis. Unsupported encodings/accesses
are diagnosed and rejected. Valid alternate modes disconnect GPIO but diagnose
the unimplemented peripheral route; UART chardevs remain independent test paths.

The reset profile selects GPIO, enables inputs and removes pulls, retaining
external driver state. It is a RAM-test profile, not a silicon reset claim.
RST_N chip reset, clock gating, low-power halfword access and alternate UART/SPI/
SDIO signal routing remain unimplemented. `-M ws53,gpio-direct=on` disconnects
the pad model only for isolated GPIO register tests. The integrated tests check
ten behavior classes plus eleven expected unsupported-access diagnostics.

The optional `gpio-smoke` RAM image first configures all 28 non-debug, non-reset
pads through HAL PAC accessors. Each `WS53 PAD NN READY` handshake checks the
target mux/pad addresses against the independently SDK-checked fixture. Input
disabled rejects GPIO ownership transfer; enabled mode 0 allows it. These full
pad sweeps are simulation-only and must never be run on the physical board.

The image then exercises the HAL's borrowed Input/Output
drivers and `embedded-hal` digital traits on GPIO0 bit2, GPIO1 bit3 and GPIO2
bit2. `scripts/ws53_gpio_ram_test.py` supplies external digital inputs through
qtest and synchronizes with UART2. The 66 ordered stages verify output preload,
set/clear/toggle, input sampling, unchanged neighboring configuration, six real
ISR notifications per bank (IRQ34/33/29), and cancellation with an edge already
pending. Both explicit release and Drop are checked; a held active level must
not cause an interrupt storm after the one-shot handler masks the local IRQ.

```sh
uv run --script scripts/test-ws53.py --qemu /path/to/qemu-system-riscv32 \
  --elf-gpio /path/to/gpio-smoke.elf --elf-all /path/to/all-features.elf
```

`--elf-all` expects all UART and GPIO features together. `--elf-uart-all` still
means only the three UART features. These explicit injection images are for
QEMU, not the default hardware RAM test. No physical pad mux, clock handoff,
electrical behavior, debounce timing or GPIO silicon qualification is implied.

The application NMI aggregation window is at `0x52000700`, following
`hisi-registers` 4939cd2 and the SDK's selected `non_os` source profile. It accepts
only halfword accesses at enable/raw/masked offsets 0/4/8. Five named source
positions (bits1..5: WDT, XIP control, XIP cache, MDMA, SMDMA) drive raw status;
enabled active sources produce the `pending` output. Reads do not acknowledge
sources. Peripheral-side latching belongs to the source device and is not
invented by this block. The deterministic RAM reset profile disables all
enables but retains externally driven levels; this is not a silicon reset claim.

`scripts/ws53_nmi_test.py` checks all 1024 source/enable combinations, held
sources across mask/reset, last-source release and 16 invalid accesses/signals.
Wrong widths, holes, unmodeled bits and status writes are diagnosed and leave
state unchanged. Tests inject source levels via `/machine/nmi source`; the WDT
device now drives source bit1 and clears it on peripheral reset. The aggregate's
held-input reset test uses independent source bits2/4. These source names alone do not establish
usable DMA engines or application ownership.

**CPU NMI delivery is unimplemented.** The output is deliberately not connected
to the ordinary local IRQ controller, and the first asserted output emits an
`unimplemented CPU NMI delivery` diagnostic. Only the named aggregation tests
and the explicitly named watchdog model tests expect this diagnostic; RAM success paths still reject every unimplemented
access. The model neither claims CPU entry/return, NMI nesting/maskability nor
watchdog SoC reset behavior. It must not silently make NMI-using firmware pass.

## TCXO application counter and snapshot model

`ws53-tcxo` maps only `0x57000200..0x57000213`: the adjacent control-core window
is not modeled. The five 32-bit slots follow RDL 4939cd2 and SDK 163d9150.
The four count slots expose a single captured 64-bit counter, 16 bits per slot.
Refresh invalidates stale data before completion; invalid snapshots, wrong
widths, unknown commands, RO writes and reserved offsets produce diagnostics.
Clear explicitly starts a new epoch; enable preserves the inherited epoch.

The default 32 MHz input is the SDK reference configuration, not a measurement
or clock-register model. `clock-frequency` accepts any nonzero u32 frequency.
Counting uses QEMU virtual time with fractional cycles preserved across pauses;
64-bit wrap is intentional. Test-only `clock` and `stall-snapshot` inputs on
`/machine/tcxo` exercise stop/resume and incomplete snapshot handshakes. A pending
request completes when both conditions allow it. Disable pauses this model;
its physical semantics and latency are not claimed as verified silicon facts.

`initial-count` seeds an inherited fixture epoch. `sticky-commands=on` retains
the last written command bits on reads; the default returns zero for them.
Both alternatives are tested so the HAL cannot rely on speculative self-clear
timing or accidentally replay CLEAR during refresh. Immediate completion and
model reset (disabled, invalid, seeded epoch) are deterministic test behavior,
not silicon timing or reset values. External clock/stall inputs survive reset.

`scripts/ws53_tcxo_test.py`, included in the standard WS53 model suite, checks
stable snapshots, counter assembly, valid zero, clear, fractional clocks,
stall/clock recovery, wrap across all data-word boundaries, extreme rates,
both command-readback profiles and invalid accesses. The parent repository's
`tcxo_control` RAM fixture additionally runs the actual Rust HAL in four
scenarios: each command-readback profile, stopped input clock and stalled latch.
No physical device is accessed. Control-core coordination, ROM execution,
automatic clock setup and LPM compensation are not implemented by this device.

## RTC two-channel counter and explicit timing profile

`ws53-rtc` maps application channels `0x57029100` and `0x57029200`, with IRQs
49/50. The SDK profile selects 32-bit counts; upper-word/common-bank accesses
are diagnosed, not silently supplied. CONTROL ready/status fields are read-only;
EOI only accepts the source driver's write-one command. Unknown RSTFSM effects,
live LOAD/mode changes, invalid widths, unsampled counts and holes are rejected.
No register reset values or board clock provisioning are inferred.

Counter/mask control is rejected unless a named test enables
`-global ws53-rtc.assume-timing=on`. The first such control access per channel
prints the assumed profile: 32768 Hz, expiry after count+1 ticks, active-high
mask, reload on enable, one-shot modes 0/2, periodic mode1 reloading LOAD, and
free-run mode3 wrapping to u32::MAX after its first expiry. These conventions
are explicit modeling assumptions, not silicon evidence. Start is immediate in
this profile; the HAL's 65/32 us synchronization waits are not measured hardware
latencies. Counts retain fractional cycles across input-clock pauses.

Each channel exposes qtest inputs `clock`, `stall-snapshot` and test-only
`expire-on-request` on `/machine/rtc0` or `/machine/rtc1`. The last input expires
a one-shot between the guest's CONTROL read and its request write, once. This
reproduces stale ENABLE being written back and rearming the counter. The HAL
fix protects the request and pending check from ISR acknowledgement, stops an
expired one-shot without consuming its event, restores IRQ state, then waits
32 us. The Rust fixture verifies both channels return Expired and still deliver
one original IRQ each. Physical status-propagation timing remains unverified.

`sticky-request=on` keeps request readback set; the default reads it as zero.
Both profiles test the HAL's avoidance of unrelated request replay. A fresh
request invalidates old data until the enabled, clocked, unstalled channel can
latch. Snapshot reads then remain stable while the counter advances. Model
reset disables channels and clears owned IRQ state, retaining external fault
inputs. This is a deterministic fixture reset, not a silicon reset claim.

`scripts/ws53_rtc_test.py` covers both channels/masks/IRQs, modes0/1/2/3,
zero/max loads, fractional pause, latch recovery, stop/restart, channel isolation,
the stale-control expiry race and 44 rejected accesses. The parent `rtc_control`
fixture adds six actual Rust scenarios: normal and sticky request readback,
clock stall, snapshot stall, expiry race under each readback profile. These
exercise real ISR entry/return and already-pending stop/Drop cancellation.
Shared clock/reset ownership, ROM code, common-bank/64-bit support and sleep or
physical recovery are not implemented or certified by this model.

## Watchdog command and assumed-timing model

`ws53-wdt` at `0x5702b000` implements 32-bit command/status views from RDL
4939cd2. `scripts/ws53_wdt_test.py` is included in the normal model regression.
Unlock/restart use `0x5a5a5a5a`; EOI consumes a read and never infers an independent
write-clear effect. Raw status survives masking and feeding. Snapshot requests
clear validity until completion, and counter reads consume the captured value,
not a continuously changing counter. The WDT output connects to NMI source bit1.

The SDK does **not** establish the initial low counter byte, zero-expiry edge,
reload between stages, or the stage effect of EOI versus kick. Therefore the
default device rejects reload/counting/snapshot requests with an `unimplemented`
diagnostic. It must not silently supply guessed watchdog timing to firmware.
Only explicit tests may select:

```sh
-global ws53-wdt.assume-timing=on -global ws53-wdt.assumed-load-low-byte=255
```

This opt-in profile uses an inherited 32000 Hz clock, expiry after `count+1`
ticks, reload after a non-reset expiry, kick restarting the first stage without
clearing raw status, and EOI clearing raw status without restarting the stage.
Enable reloads the counter; disable freezes its remaining interval. LOAD's
reserved low byte reads zero; the independently assumed counter low byte is
0..255 (default255), swept at both extremes by tests. These are explicit model
assumptions, **not new silicon facts**. One diagnostic announces the profile on
first use. Counts at `u32::MAX`, valid zero and fractional clock pause/resume are
tested without wrapping the duration arithmetic.

Test-only named inputs `/machine/wdt clock 0` and `stall-snapshot 0` pause the
input clock and block snapshot completion. Resuming the clock retains fractional
tick phase. A stalled snapshot never returns ready; releasing the stall completes
the pending request. System reset clears model state and owned interrupt outputs
but retains external clock/stall levels. Initial locked/disabled/zero state is a
deterministic fixture initialization, not a claimed hardware reset value.

The `reset-request` output is observable, but **SoC reset topology and pulse
timing are unimplemented**. Assertion diagnoses that gap and stops counting until
the fixture resets the device; it does not reset a CPU behind the guest's back or
pretend a reset completed. The pulse-length register is retained but not timed.
CPU NMI delivery likewise remains explicitly diagnosed by the aggregation model.
Wrong widths, holes, write-only reads, read-only writes, unsampled CNT, unknown
commands, LPIF/busy state and writes under unknown lock protection are rejected
with diagnostics. In particular this is not a complete low-power/reset model or
permission to run a physical watchdog test. Named model tests accept only their
exact expected diagnostics; ordinary RAM tests still reject them.

The shared Python qtest context now sends QMP `quit` with its qtest channel
still open. This avoids racing socket closure/SIGTERM against QEMU's log timer
teardown; strict NMI diagnostic checks retain no unrelated host warnings.

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

## PWM channel model with explicit timing assumptions

`ws53-pwm` maps the four groups/eight channels at `0x52082000`, IRQ71/72,
plus only the PWM gate halfword at `0x52000040` and divider halfword at
`0x5200007c`. The rest of M_CRG remains unimplemented. Source layout is SDK
`163d9150` / RDL `f188fd4`; no chip reset values are inferred. Unsupported
widths, holes, RO writes, WO reads, nonzero high count halves, DMA enable and
unknown divider profiles are diagnosed and rejected.

START commands require `-global ws53-pwm.assume-timing=on`. Its one-time
notice declares the assumed 32MHz/low16 count profile, immediate clear-start,
and deferred commit at the next block end. These latch, counter, mask and
output-enable conventions require silicon verification. Divider16 requires
a rising LOAD command and enable/gate/input clock; pauses preserve fractional
ticks. No undocumented upstream divider relationship is inferred.

Each channel has shadow/active/deferred configuration, phase and block counters,
output polarity/enable, sticky completion and independently staged stop. The
model emits named `out` and `output-enable` channel signals, without mapping
them to guessed physical pads. Group/count configuration errors feed IRQ71;
step-end flags feed IRQ72. Masked events remain latched until selected W1C
acknowledgement. Invalid group commits are atomic in this profile. Reset clears
owned state and timers but retains the external `/machine/pwm clock` fault.

`scripts/ws53_pwm_test.py` is part of the WS53 suite. It covers all groups and
channels, shadow updates, fractional edges, zero/full duty, polarity, concurrent
finite/repeating outputs, mask/ack, both error sources, clock pause/recovery,
16-bit count wrap, maximum block, reset and 29 rejected MMIO operations. It
also exposes the late-stop hazard: with a short block the SDK's 1us two-commit
sequence has already emitted extra blocks before the second command.

The parent's QEMU-only `pwm_control` Rust fixture verifies actual generated PAC
access, all eight completion flags via ISR72, duty traits, route retention and
Drop. The HAL now conservatively returns `FiniteDeadlineMissed` and stops the
whole group if a finite completion flag appears during staging. Both PWM IRQs
are suspended for that transaction; unrelated interrupts remain enabled.
The check may reject a block that ended just after the stop command, cannot
undo pulses, and does not prove silicon status-propagation timing. A second
fixture injects stopped clock and checks absence of events plus cleanup;
`start()` does not detect an absent clock. Synthetic pad permissions in these
fixtures are not a usable board pin map. Physical output, clock ownership,
control-core cooperation, DMA and silicon stop latency remain unverified.

## I2C master functional profile

`ws53-i2c` implements independent non-PRO v151 controllers at `0x52083000`
and `0x52084000`, IRQ62/63, and M_CLKEN1 halfword `0x52000044` bits6/7.
Register layout and accesses are from SDK `163d9150` / RDL `01dcce5`.
Unmodeled clock bits cannot be changed; inherited TIMER/UART bits remain set.
This is a boot handoff profile, not asserted silicon reset values.

Transfers require `-global ws53-i2c.assume-profile=on`. The diagnostic declares
32MHz, eight-entry TX/RX FIFOs, disabled-controller CON address selection,
read ACK deferred across an empty command FIFO, and 1us enable/disable crossing.
These timing/ACK/disable assumptions are **not measured hardware facts**.
A STOP or direction-changing RESTART terminates the prior read with NACK;
contiguous reads acknowledge all but the last byte. Clock pauses preserve the
remaining event interval; bus stretching stalls transfer but allows disable.
The model does not emulate physical SCL/SDA, pinmux, multi-master wire contention,
or SDA hold/setup effects. Speed3 uses the SDK's HS slots as Fast-plus; no
high-speed master-code protocol is claimed.

Each instance has its own volatile 256-byte register peer, selected by 7-bit
0x50 or 10-bit 0x250; these are synthetic test addresses, not board devices.
The first write byte after START/RESTART selects a wrapping byte pointer, later
writes store data and reads advance it. Initial memory is `index ^ 0xa5`.
The peer generates address NACK for other addresses. Named external inputs at
`/machine/i2c` (`clock`, `stretch`, `nack-address`, `nack-data`, `arbitration`,
indices0/1) inject faults and survive system reset. Read-only QOM counters
`startsN`, `restartsN`, `stopsN`, `read-acksN`, `read-nacksN` expose protocol
observations without inventing guest registers. Reset clears controller/peer
state; it does not represent a control-core firmware boot.

FIFO thresholds, TX overflow, RX under/overflow and full-RX hold are modeled;
raw events and abort details remain until write-one clear-all at +0xc4. Selective
clear, SMBus, slave mode, DMA, dynamic TAR selection, unknown fields/holes and
invalid widths are rejected with guest-error diagnostics. Masks only accept
implemented event sources. Explicit ABORT flushes FIFO and retains its source.

`scripts/ws53_i2c_test.py` covers both instances/address modes, data integrity,
three speed count profiles, long reads, repeated START, final-read NACK, FIFO
capacity/hold, IRQ62/63 mask/clear, abort/NACK/arbitration, stopped clocks,
stretching, reset and 38 rejected MMIO operations. The parent `i2c_control` RAM
fixture executes real Rust HAL/PAC transactions, 100-byte reads, address-NACK
recovery, resource release/reacquisition and a clock-stall/other-instance case.
Synthetic pad permissions in that fixture must never be used on a physical board.
I2C IRQ-driven HAL, DMA/async and physical electrical/timing qualification remain
outstanding; model passes are not stable-API graduation evidence.
