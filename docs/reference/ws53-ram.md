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
An opt-in UART0/1 DMA profile adds a FIFO and serializer with assumed timing;
physical TX timing, flow-control and DMA validation remain outstanding. None of these results prove
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
output polarity/enable, sticky configuration-load notifications and independently staged stop. The
model emits named `out` and `output-enable` channel signals, without mapping
them to guessed physical pads. Group/count configuration errors feed IRQ71;
Configuration-load flags feed IRQ72, including initial finite START. Masked events remain latched until selected W1C
acknowledgement. Invalid group commits are atomic in this profile. Reset clears
owned state and timers but retains the external `/machine/pwm clock` fault.

`scripts/ws53_pwm_test.py` is part of the WS53 suite. It covers all groups and
channels, shadow updates, fractional edges, zero/full duty, polarity, concurrent
finite/repeating outputs, mask/ack, both error sources, clock pause/recovery,
16-bit count wrap, maximum block, reset and 29 rejected MMIO operations. It
also exposes the late-stop hazard: with a short block the SDK's 1us two-commit
sequence has already emitted extra blocks before the second command.

The parent's QEMU-only `pwm_control` Rust fixture verifies actual generated PAC
access, all eight load notifications via ISR72, duty traits, route retention and
Drop. The HAL now conservatively returns `FiniteDeadlineMissed` and stops the
whole group if another load notification appears during staging after the initial load flag was acknowledged. Both PWM IRQs
are suspended for that transaction; unrelated interrupts remain enabled.
The check may reject a block that ended just after the stop command, cannot
undo pulses, and does not prove silicon status-propagation timing. A second
fixture injects stopped clock and checks absence of events plus cleanup;
`start()` does not detect an absent clock. Synthetic pad permissions in these
fixtures are not a usable board pin map. Physical output, clock ownership,
control-core cooperation, DMA and silicon stop latency remain unverified.

## I2C master functional profile

`ws53-i2c` implements independent non-PRO v151 controllers at `0x52083000`
and `0x52084000`, IRQ62/63, and M_CLKEN1 halfword `0x52000044` bits6/7
from the shared `ws53-mclken1` provider (SPI0 uses bit11).
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

## SPI0/QSPI1 single-line functional profile

`ws53-spi` uses separately audited v151_100 layouts: SPI0 at `0x52087000`
(IRQ65), QSPI1 at `0x5208d000` (IRQ43). Register facts come from SDK
`163d9150` / RDL `04ced9c`. ENABLE/CONTROL/FIFO/status/IRQ bit positions differ;
all 36 FIFO address aliases access the same queue. There is no catch-all MMIO.
Version/identification values are unknown and reads report a diagnostic.

Transfers require `-global ws53-spi.assume-profile=on`. The diagnostic states
assumed 32MHz, eight-entry TX/RX FIFOs, 1us enable/disable crossing and a virtual
full-duplex XOR peer. Each transmitted word determines a received word via
`(MOSI ^ 0xa5a5a5a5) & word_mask`; it is not a preselected success response.
Supported frames are 8/32 bits, MSB-first single-line Motorola master, modes0–3.
The virtual peer does not produce edges, so CPOL/CPHA/sample delays are stored
but do not constitute waveform validation. Frame time uses width and even BAUD
divider; pause/resume preserves the remaining time. FIFO8 and disable flushing
are explicit assumptions, not inferred from the 36 windows or silicon tests.

TX/RX thresholds drive level IRQs; overflow, underflow and collision are latched
until the respective read-clear or clear-all access. Writes to read-clear registers
have no effect, as described by the source. Clearing a latch cannot suppress a
still-active FIFO level. IRQ mask/raw/masked views, BUSY and FIFO levels change
with actual queued transfers. Software reset cancels timers and clears queues;
it does not emulate the vendor control-core startup.

The sole `ws53-mclken1` provider owns `0x52000044`, feeding I2C0/1 gates bits6/7
and SPI0 gate bit11. Its inherited TIMER/UART profile bits remain unchanged.
SPI0 additionally needs SPI_CLOCK and master selection; QSPI1 needs GLOBAL_CLKEN,
XIP_QSPI_CLOCK control=1 and all three XIP_CLOCK_ENABLE path bits. Non-profile
field changes and master/slave changes while active are diagnosed. XIP execution,
clock-source switching, shared-user coordination and sleep acknowledgements are
not modeled. There are no overlapping per-controller mappings for M_CLKEN1.

Named inputs `/machine/spi` `clock`, `stretch`, `collision`, `disable-stall`
(indices0/1) inject independent faults and survive reset. `stretch` pauses words
but permits disable; stopped `clock` also stalls disable. Enable register readback
alone is not an independent clock acknowledgement: a queued word can remain BUSY
when the requested enable bit has already cleared. Read-only QOM `frames0/1`
counts completed frame events, including failed collision frames.

`scripts/ws53_spi_test.py`, included in the standard suite, checks both layouts,
8/32-bit frames in four modes, 100-word transfers, every FIFO alias, IRQ65/43,
FIFO overflow/underflow, read-clear writes, every required gate, external faults,
reset, shared-I2C gate independence and 41 invalid MMIO/configuration diagnostics.
The parent's `spi_control` RAM fixture exercises actual HAL/PAC transactions,
word widths/modes, unequal/in-place transfers, ownership return/reacquisition,
and stopped-clock poisoning with resource retention and the other bus working.

This is partial single-line support. Dual/quad protocols, native-CS continuity,
DMA, slave/Microwire/DDR, IRQ-driven HAL/async, physical routes and waveform timing
remain unimplemented or unverified. Unsupported features produce guest errors.
The synthetic fixture pads are not board wiring; never run it on a physical board.

## Temperature polling functional profile

`ws53-tsensor` maps the six source-audited registers at `0x59006300`; its
layout/access evidence is RDL `04ced9c` and SDK `163d9150`. The complete selected
C port and 1024-code conversion oracle are checked in hisi-registers `37db3cd`.
There is no evidenced temperature IRQ, stop command, READY-clear operation or
clock-gate register, so none is invented by this model.

START requires `-global ws53-tsensor.assume-profile=on`, all four reference
configuration writes (period160, CONTROL3, sample-time2, refresh-config0) and
START1. Only these source-profile values are accepted. The explicit diagnostic
states assumptions: sixteen 256us sample points, integer-floor average, reports
at 5ms intervals after the first 4.096ms report, sticky READY/latest result and
START restarting/invalidation. These temporal/averaging/latch semantics are not
silicon measurements; the C port's comments do not fully specify them.

The controlled ten-bit input defaults to500 and may be set with
`-global ws53-tsensor.initial-code=933` or `/machine/tsensor` GPIO input `code`
bits0..9. Every sample uses the current input, so changing input halfway through
an average affects the result. No nominal-temperature clipping occurs in the
model: all1024 raw values can be reported. Conversion to nominal Celsius belongs
to the HAL, with reference-range and calibration limitations kept explicit.

Named inputs `clock` (high=running), `stall` and `corrupt-control` pause/resume
sampling while retaining partial sum and remaining interval. The corruption
input exposes CONTROL=0xdead and rejects writes; it is a fault injection, not an
invented hardware bit. Once READY has latched, loss of clock leaves the old
sample readable. A successful read therefore does not establish freshness or
clock health. Reset cancels timers, partial averages and READY; external input
and faults persist. This reset profile does not emulate vendor thermal startup.
Read-only QOM counters `starts`, `samples`, `reports` permit test observations
without adding guest registers. Unsupported access widths, configuration values,
write-only reads and status writes report guest errors.

`scripts/ws53_tsensor_test.py` checks all1024 inputs, changing-input averaging,
rounding, deadlines, sticky stale samples, clock/stall/control faults, partial
reset and20 invalid accesses. The parent `temperature_control` fixture runs the
real HAL/PAC with reference code933, out-of-reference code1023, clock stopped
before first readiness and unknown CONTROL. It verifies bounded timeout/retry,
retained permissions and no restart on reattachment using QOM counters. These
are digital-contract tests, not calibrated sensor, power or electrical evidence.

## I2S stereo functional profile

`ws53-i2s` maps the WS53-specific SIO v151 registers at `0x52030000`,
AUDIO_CMU_NEW_CFG0 at `0x57008600`, and IRQ67. Register layout and width codes
come from SDK `163d9150` and hisi-registers `471ef7c` (C source-contract oracle).
Width codes **1..5** mean 16/18/20/24/32 bits; zero is reserved. These are not
WS63's register definitions. Enable `-global ws53-i2s.assume-profile=on` to run
transfers; otherwise enable commands fail with an explicit diagnostic.

The opted-in **unverified digital assumptions** are:

- Stereo I2S, either edge and master/slave roles; four independent eight-word
  FIFOs. Merged FIFOs, PCM, additional channels, signed extension, loopback,
  alternate clock selectors and DMA are rejected rather than emulated as success.
- Master uses a32MHz source. One frame takes `ceil(div * 2 * width *1e9 /32e6)`
  ns, where divider is1..127 and FS ratio equals width. Slave has a virtual peer
  period (`slave-period-ns`, default100000ns) and independent `slave-clock` input.
  There is no audio waveform, physical pin routing or exact silicon timing proof.
- A separate serializer takes one pair from TX at frame start and completes it
  later. FIFO-empty therefore precedes wire completion. Continuous clocks with
  empty left/right TX latch independent underflow sources. Missing samples use
  zero and retain errors; the model does not pretend the transaction succeeded.
- Duplex peer returns left TX XOR`0xa5a5` and right TX XOR`0x5a5a`, masked to the
  configured width. RX-only peer sends the completed-frame counter and its inverse.
  Each full RX side drops its new sample and latches its own overflow bit.
- Raw INTSTATUS reports sticky errors and live thresholds (RX both depths above
  RX threshold; TX maximum depth at/below TX threshold). INTCLR acknowledges
  sticky bits; threshold levels reassert. INTMASK one masks, CT interrupt-enable
  gates IRQ67. The raw-status and threshold details remain model assumptions.
- CT_SET/CLR are write-only commands. Channel reset discards FIFOs, partial frame
  and errors but preserves config. Stop/direction changes discard a partial frame,
  never establish lossless drain. System reset clears config/counters as model
  policy, not documented silicon reset values.
- Gate, CMU bit0, CRG bit1 and external `clock`/`stall` pause frame progress and
  preserve remaining time. `reset-stall` keeps queued data through channel reset
  for recovery-failure tests. External clock/fault inputs survive system reset.

The new `ws53-mclken0` provider is the single register backing PWM's16-bit and
I2S's32-bit M_CLKEN0 views at `0x52000040`. Bit7 drives PWM, bit2 drives I2S.
Other bits are inherited opaque state (`initial-value` test property); attempts
to change them diagnose unsupported behavior. Halfword writes preserve the upper
half. This checks RMW interoperability without inventing additional peripherals.

`scripts/ws53_i2s_test.py` verifies all20 width/role/edge combinations with101
continuous frames each, divider endpoints, data-dependent replies, short RX,
FIFO depth, separate serializer, independent faults, IRQ masks/W1C/thresholds,
clock pause/resume, reset failure/recovery, shared PWM clocks and39 invalid
access/configuration diagnostics. It is part of `scripts/test-ws53.py`.
The integration repository's `examples/ws53/i2s_control` runs the actual Rust
HAL/PAC in RAM, with normal and stopped-clock fixtures. Its unsafe pad/ownership
handoffs and external quiescence proof apply **only to this model**. Neither
qtest nor these RAM fixtures count as physical I2S acceptance.

## ADC v155 analog calibration profile

`ws53-adc` maps the audited calibration subset at `0x5208a000` and ADC reset
bit13 at `0x52000054` (32-bit). It requires explicit
`-global ws53-adc.assume-profile=on`. Source facts come from SDK163d9150,
RDL471ef7c and the b4b1330 executed C source contract. Automatic scan and MCU_DIAG
capture require the additional profile below; manual sampling remains unsupported
and diagnosed. The SDK's empty IRQ80 handler supplies no event or
acknowledgement contract, so no ADC interrupt is invented.

The model requires the source analog word values and ordered enable sequence
`0x260 -> 0x103f8 -> 0x1ffff`. Its explicit **unverified digital assumptions** are
5us settling before the second step, calibration completion after100/128/160us,
and clear/refresh commands taking effect on a1-to-0 pulse edge. Configuration
and stage prerequisites matter: automatic offset must finish before corrected
manual offset, then manual offset must finish before capacitor calibration.
A readable FINISHED value is not supplied merely because ENABLE was written.

An input `offset-code` property (six bits; default10) and six `code` GPIO inputs
control the automatic result. The source piecewise manual correction is checked
before its stage starts; CAP_STATUS can complete with both FINISHED and ERROR.
`clock`, two upstream gates and per-stage `stall` inputs pause remaining time;
`stale` prevents completion clear, and `cap-error` injects capacitor failure.
Disabling a stage or asserting reset cancels pending work. Register-enable
readback, timing, reset effects and no post-disable completion are model
assumptions, not physical shutdown or calibration-accuracy evidence.

Shared clock ownership is explicit: `ws53-mclken1` now also routes ADC bit2,
while the new `ws53-global-clken` backs QSPI bit7 and ADC bit12 at0x52000548.
Both use16-bit access; RMW preserves independent consumers. Other GLOBAL_CLKEN
bits are opaque inherited state (`initial-value` property) and cannot be changed
to pretend that additional peripheral gates work. These providers do not assert
control-core ownership or model real analog power rails.

`scripts/ws53_adc_test.py` covers64 input codes ×3 stages, exact completion
boundaries,5us settling,1-to-0 clear behavior, every stage stall/cancel/retry,
independent clocks, CAP_ERROR, stale status, reset, concurrent SPI0/QSPI1 progress,
and31 invalid accesses/configurations. It is included in the full WS53 suite.
The integration repo's actual Rust/PAC `adc_calibration` RAM fixture runs eight
boundary codes ×20 complete calibration sequences and five fault scenarios;
QOM counters and register reads verify progress and final disabled calibration
controls. It never starts a scan, changes physical pads or programs diagnostic
RAM. Capture/ownership/cache/drain, physical inputs and true analog behavior
remain open and must not be reported as supported by these tests.

## ADC to MCU_DIAG capture profile

Add `-global ws53-adc.assume-capture=on` alongside `assume-profile=on` to test
automatic scans into real emulated DTCM. `scripts/ws53_adc_capture_test.py` is
included in `scripts/test-ws53.py`. This is a digital test profile, **not proof of
silicon conversion timing, end-address semantics, coherency or bus ordering**.

The model supports the source scan settings (average8, sample6, start26,
discard32), all nonempty eight-channel masks, successful calibration prerequisites,
and MCU_DIAG at `0x52004100`. Only ADC source6 and sync-enabled once/repeat modes
are implemented. It requires explicit monitor/save/clock setup and rejects active
reconfiguration. CPU trace is inherited ownership metadata (`initial-trace=0..3`),
not an emulated trace producer; capture arming fails until that metadata is cleared.

Explicit assumptions and test inputs:

- Length counts32-bit words; aligned start and **exact inclusive end byte** must
  describe a nonempty interval wholly inside `0x20010000..0x20060000`. A64-bit
  range calculation rejects wraparound, MMIO destinations and oversized buffers.
- Channels are sampled round-robin. Each channel has12 external code bits on
  the `input` GPIO bank (`channel*12+bit`), with initial codes100,200,...800.
  Result words contain code in bits2..13 and channel in bits14..16. These are raw
  code inputs, not a voltage/averaging/analog calibration model.
- A conversion (`sample-period-ns`, default10000) creates a separate pending
  RAM write (`write-latency-ns`, default5000). Both properties accept1..1e9ns.
  Conversion waits for the previous write; this is not a throughput guarantee.
- ADC stop, diagnostic disable and ADC soft reset cancel future conversion but
  **retain an already posted RAM beat**. ADC gates/source clock cannot retract
  it. Diagnostic clock loss or `bus-stall` holds it pending. Machine reset
  cancels both stages as model policy, not an asserted physical reset guarantee.
- Normal DONE follows the final committed write. Repeat mode wraps and keeps
  DONE sticky while new writes continue. `early-done` deliberately exposes DONE
  and final address **before** RAM commit. This adversarial scenario prevents a
  driver from treating these reads as sufficient buffer-release evidence.
- `sample-stall`, `clock` and `diag-clock` pause conversion with exact remaining
  time; `diag-clock` and `bus-stall` pause the writer. `bus-error` rejects its
  write, withholds DONE and emits a diagnostic; no undocumented architectural
  error flag or IRQ is invented. External codes/clock/faults survive reset.

QOM counters `capture-in-flight`, `capture-words`, `capture-cycles` and
`capture-errors` are test instrumentation, **not guest registers or a physical
drain API**. Tests inspect real RAM and guard words for all255 channel masks,
both DTCM ends and the full320KiB aperture (including high length bits), exact
pause/resume, cancellation, late writes, repeat mode, failed bus writes/recovery,
34 invalid accesses/configurations and six invalid property sets. No test here
establishes application/control-core ownership, cache safety, physical pad
routing, or a safe HAL capture buffer release operation.

## ROM v151 MDMA memory-transfer profile

`ws53-mdma` maps the actual WS53 eight-channel layout at `0x52070000`, local
IRQ58, with0x20 channel stride and LLI/DEST/CFG/SOURCE/CONTROL offsets0/4/8/16/20.
It does not reuse the WS63 device layout. SDK163d9150 and source-contract commit
`f13eaaf` verify register fields/helpers, not physical transfer/drain semantics.
SDMA remains absent: its macro does not establish a selected application driver.

Transfer execution requires `-global ws53-mdma.assume-profile=on`. The profile
implements DTCM-to-DTCM8/16/32-bit elements with equal source/destination widths,
counts1..4095, independent fixed/incrementing addresses, one-beat bursts and
master0/protection0. Both ranges must fit `0x20010000..0x20060000` and align to
width. Actual address-space reads and writes move data; failure latches an error
instead of setting success. Wrong widths, alignment/ranges, bursts, masters,
flow-control, request routing, nonzero LLI and unknown registers are diagnosed.
It is not yet a complete DMA model: non-UART peripheral pacing, LLI/circular chains,
arbitration priorities, packing/conversion widths and NMI fault routing remain
unimplemented. Software request/SYNC registers are also rejected explicitly.

Unverified digital assumptions, visible at the profile boundary:

- Each element has a separate source-read and destination-write event, each
  `phase-ns` (default500, accepted1..1e9). Eight channels progress independently;
  this makes no claim about physical bandwidth or arbitration.
- ACTIVE reflects an already read element waiting to be written. Element count
  decrements and incrementing addresses advance only after successful write.
  Terminal completion clears channel enable, and latches raw TC if CONTROL bit31
  is enabled. Channel mask bits12/13 use1=unmask; raw bits persist until W1 clear.
  Shared IRQ58 reflects any unmasked TC/error, independently of controller enable.
- Halt/channel-disable/controller-disable prevent the next source read, but do
  **not** retract an already read/posted destination beat. Reconfiguration or
  rearming cannot redirect that outstanding write. Halt/controller reenable
  resumes retained progress. A disable can restart remaining count only after
  its posted beat has completed. No HAL buffer-release proof is inferred.
- External `clock` and per-channel `stall` freeze both phases with exact remaining
  time. Per-channel `bus-stall` freezes only a pending write. `read-error` and
  `write-error` inject transaction failures; failed data/count/address are retained,
  channel enable clears and raw error latches. Other channels continue.
- Machine reset cancels events and clears registers/counters as model policy;
  external faults persist. This is not evidence of physical reset/drain behavior.
  Clock-tree mapping is not fabricated: the explicit profile inherits a running
  bus clock and provides the named external clock input for tests.

QOM `reads`, `writes`, `errors`, `transfers` and `pending-writes` are test-only
counters. `scripts/ws53_mdma_test.py` covers96 channel/width/increment combinations,
8 concurrent channels, full4095-element boundaries, exact pause/resume, real RAM,
IRQ masking/W1 clearing, both transaction failures, cancellation/posted writes,
reset/recovery,37 invalid MMIO/configuration diagnostics and explicit opt-in.
It is part of `scripts/test-ws53.py`. The integration repository now has a polling
memory-copy HAL and actual Rust RAM fixtures; their completion observation never
proves physical cache/drain safety, and hardware tests remain separate work.

## UART0/1 request-paced MDMA profile

Enable both `-global ws53-mdma.assume-profile=on` and
`-global ws53-mdma.assume-uart=on`. The second option configures UART0/1 with
an explicit FIFO64/timed-serializer/byte-handshake assumption; UART2 retains the
legacy console model and has no DMA endpoint. Request10/11 selects UART0 TX/RX
DATA at0x52081004; request8/9 selects UART1 at0x52080004. These routes and DATA
offsets come from SDK163d9150, independently checked by hisi-registers `e8e8ce0`.
Flow1/2 is DMA-controlled memory-to-peripheral/peripheral-to-memory; only byte
width, incrementing DTCM memory, fixed peripheral address, one-beat burst and
master0/protection0 are accepted. Wrong direction/address/request, foreign
protection, width conversion, request changes during transfer and UART2 are
rejected, not converted to memory copies or success stubs.

The source audit found conflicts: the SDK writes UART_PARAMETER DMA_EXTRA even
though its own header marks that capability RO, and its UART BUFFERABLE literal
is1 while the DMA HAL enum is2. UART_PARAMETER stays RO; no guessed enable write
or bufferability equivalence is implemented. The opt-in profile advertises
DMA_EXTRA=1, inherited active clocks and exclusive resource ownership. This is
not evidence of the connected board's actual capability; reference product
configuration disables UART DMA.

Explicit behavioral assumptions:

- UART TX has a64-byte FIFO plus an independent shift byte. `char-ns` defaults
  to10000 (range1..1e9), unrelated to the inherited physical baud divisor. TX
  counter excludes the active shift byte; LINE_STATUS TX_EMPTY only asserts when
  both are empty. Loopback bytes arrive only after serialization. HALT_TX stops
  new shift bytes; FIFO reset clears queued bytes but preserves the active one.
  FIFO_STATUS bit0 means FIFO full and bit1 means FIFO empty, independently of
  the active shift byte. A regression checks the empty-but-still-transmitting
  interval, queued data, and final LINE_STATUS TX_EMPTY.
- TX request means at least one FIFO slot; RX request means at least one byte.
  These are single-byte level requests, not a model of burst thresholds,
  DMA_EXTRA hardware acknowledge, software ACK or physical APB lane wiring.
  The dedicated endpoint byte adapter leaves CPU MMIO32-bit. DMA cannot read
  empty RX as zero or overwrite full TX silently. Request withdrawal prevents a
  new source beat. Repeated same-level notifications do not postpone its timer.
- UART `clock` and `tx-stall` inputs preserve serializer remaining time. Clock
  off suppresses requests. A previously read RX byte still reaches RAM after
  DMA cancel or UART clock removal. A TX write reaching a now-clocked-off UART
  produces a model bus error; the physical bus may behave differently. One
  channel's errors and cancellation do not clear other channels' raw events.
- DMA TC means the destination accepted the last byte, **not** UART wire idle.
  Channel halt/disable does not revoke its already-read byte. Full machine reset
  cancels active events in this model, while external injected faults persist.

`scripts/ws53_mdma_uart_test.py` is in the fullWS53 regression:32
port/channel/direction cases, both ports simultaneously transferring257 bytes
through TX/serializer/RX/DMA, FIFO backpressure, empty receive waits, exact clock
pause/resume, read/write errors, late writes after cancel, reset and46 rejected
accesses/configurations. QOM `transmitted` counts completed serialized bytes.
The integration repository separately exercises its experimental Rust UART DMA
HAL against this profile. Neither test establishes physical cache, clock, pad,
bus-drain, HPROT or control-core ownership guarantees.

## SPI/QSPI MDMA requests (explicit profile)

Enable `ws53-mdma.assume-profile=on`, `ws53-mdma.assume-spi=on` and
`ws53-spi.assume-profile=on` together. Without the SPI behavior profile no SPI
DMA channel can arm. Existing memory and UART DMA profiles retain their own
accepted widths and protection settings. This is not an implicit physical
capability probe or an installed-firmware profile.

Pinned SDK163d9150 uses SPI0 requests6/7 at DATA0x5208702c, QSPI1 requests4/5 at
DATA0x5208d060. Their DMA enable bits are reversed (TX/RX0/1 versus1/0), and
threshold registers are distinct. The source sets TXlevel4/RXlevel0 and
HPROT2 (BUFFERABLE). The model accepts only flow1/2, one-beat bursts, master0,
protection2, fixed peripheral and incrementing DTCM memory, equal8/32-bit widths
matching configured SPI frames. CPU MMIO stays32-bit; the dedicated DMA byte
adapter and physical bufferability remain assumptions. UART and memory transfers
still require protection0. No16-bit, width packing, LLI or burst ACK is modeled.

The existing FIFO8/full-duplex XOR peer is shared by CPU and DMA. TX request is
asserted while FIFO occupancy is at most the configured TXlevel and below capacity;
RX request requires occupancy greater than RXlevel. Modeled thresholds0..7 are
checked against this assumed FIFO size, not claimed silicon limits. Short RX
tails above zero threshold wait until software lowers it. Clock removal,
controller-disable and request-disable suppress new requests; clock pauses
preserve remaining serial time. Request-disable does not flush FIFOs or retract
already-read DMA beats. An accepted RX beat can still reach RAM after cancellation;
an accepted TX beat may enter the FIFO after request-disable. TX commit after
SPI clock/controller removal reports a modeled bus error. Real bus termination
may differ. DMA TC precedes serial completion; RX overflow remains observable.

`scripts/ws53_mdma_spi_test.py` runs32 port/channel/width full-duplex combinations,
two concurrent ports with257 frames each, independent memory guards and address
progress, FIFO backpressure/short tails, serial completion/clock remainder,
late TX/RX writes after cancel, read/write faults, IRQ58 isolation, reset and26
rejected accesses/configurations. It is included in `scripts/test-ws53.py`.
This step adds the model and source contracts; a Rust SPI DMA transfer interface,
physical waveform/cache/drain/CS verification and control-core handoff remain open.

## I2C MDMA requests and short tails (explicit profile)

Enable `ws53-mdma.assume-profile=on`, `ws53-mdma.assume-i2c=on` and
`ws53-i2c.assume-profile=on`. These are functional test assumptions, not measured
silicon or installed-firmware capabilities. Pinned original-source evidence is
`hisi-registers` commit `a233614`: the selected SDK application disables I2C DMA.
Actual C DATA lookup, request selectors, enable/threshold setters and master
prepare/restore agree with the existing RDL/PAC; no register description changed.

I2C0 uses DATA0x5208301c and requests0/1; I2C1 uses DATA0x5208401c and2/3.
RX accepts bytes, TX accepts bytes or halfword commands (READ0x100, STOP0x200,
RESTART0x400). CPU MMIO remains32-bit. DMA buffers must stay in DTCM; the
peripheral endpoint is fixed, RX memory increments, TX memory increments or is
fixed for repeated halfword commands. Same widths on both DMA sides, master0,
flow1/2 and source-derived TX protection1/RX protection2 are required. The latter
are encodings, **not** proof of cache/coherency or physical HPROT behavior.

The opt-in profile supports equal source/destination burst encodings0/1 (one/four
beats); other burst sizes, width conversion and LLI are rejected. Memory, UART
and SPI profiles still reject bursts. FIFO8 and request comparisons are explicit:
TX enabled and entries <= TX threshold; RX enabled and entries > RX threshold.
Thresholds0..7 are accepted; source preparation uses4/3. A new four-beat request
accepts up to `min(4, remaining_count)` credits. The accepted group continues below
the watermark, but each beat still needs FIFO data/space, an enabled controller,
clock, request enable and no abort. FIFO changes can wake an accepted group even
without crossing the watermark. No physical DMA ACK protocol, arbitration or
last-request pin is claimed by this credit model.

There is deliberately no inferred last/single request to drain a short RX tail.
With RX threshold3, lengths1..3 or a remainder below4 remain pending; software can
lower the threshold to0 and finish. A four-beat transfer with threshold0 also
waits for individual arriving bytes instead of reading zero from an empty FIFO.
Tests cover both fixed-source0x100 commands followed by a CPU final STOP command
(the SDK sequence), and incrementing halfword command arrays containing STOP.
DMA terminal completion is separate from I2C STOP, ACK/NACK and bus-idle state.

Clock loss or request disable prevents new beats, including inside an accepted
burst. Halt/disable cancels remaining credits but cannot retract a posted write.
A byte already consumed from RX may reach RAM after cancellation or I2C clock
loss. A posted TX can still enter the FIFO with requests disabled if the
controller/clock/FIFO can accept it; clock loss, disable, abort, a full FIFO or an
invalid command instead produces a DMA bus error. Peripheral NACK/arbitration
latches the I2C abort and withdraws new requests, without fabricating DMA TC.
The driver must examine **both** controllers' errors and retain resources until
independently proven quiescent. Reset discards pending model work and credits;
this is not a measured hardware reset/drain guarantee.

`scripts/ws53_mdma_i2c_test.py`, included in the fullWS53 suite, checks32
port/channel/burst read combinations,18 fixed-command reads (1..257 bytes),four
byte-write/readback sequences, two ports concurrently, and28 rejected settings.
Additional scenarios cover guards/address progress, FIFO pressure, threshold
changes, exact pause/resume within a below-watermark burst, empty-FIFO wakeup,
STOP/ACK/NACK, IRQ58/62/63 isolation, NACK/arbitration, injected read/write errors,
posted TX/RX cancellation, late FIFO-full writes, invalid commands and reset.
An I2C DMA Rust HAL and actual Rust/PAC transfer scenarios remain to be added;
passing this model does not complete I2C DMA hardware acceptance.

## I2S merge-port MDMA profile (explicit assumptions)

Enable all three options: `ws53-mdma.assume-profile=on`,
`ws53-mdma.assume-i2s=on` and `ws53-i2s.assume-profile=on`.
Source evidence is SDK `163d9150` and hisi-registers `ef2ee9b`: merge TX/RX
addresses `0x520300c0` / `0x520300a0`, requests 12/13, flow 1/2 and protection 1
in both directions. The selected SDK product disables I2S DMA. Its source does
not establish physical merge packing, request timing or legal bus lanes.

The opt-in model assumes:

- One little-endian 32-bit DATA access transfers one sample. Accesses alternate
  left/right, starting at left after reset; the five existing sample widths
  16/18/20/24/32 constrain payload bits. Byte/halfword DMA is rejected.
- Each direction keeps its merge phase across DMA completion and serializer
  stop. An odd element count completes DMA but leaves an incomplete stereo pair.
  Peripheral reset clears phase and FIFOs; it cannot retract a sample already
  consumed into an MDMA pending write.
- New requests require the explicit profile, merge, reset release, valid format
  and running clocks. TX requests use the next channel's FIFO depth `<= TX
  threshold`; RX uses `> RX threshold`. They are independent of IRQ masks.
  The original paired IRQ comparisons (MAX TX / MIN RX) remain unchanged.
- TX can prefill before starting the serializer; RX can drain after stopping
  it. Direction disable alone is therefore not a DMA request-disable operation.
  Merge switching requires a stopped serializer, empty FIFOs and complete phase.
  Split DATA accesses while merge is active fail visibly.
- Equal source/destination burst lengths 1/4/8/16 are supported. An accepted
  burst retains credits below the watermark, bounded by the DMA count; each
  sample still requires data/space and clocks. No last-request pulse is invented.
  A short RX tail below its watermark waits until software lowers the threshold.
- TX reads posted before HALT/disable can still reach the FIFO. If merge, clock
  or reset is removed before that write, MDMA reports a bus error. RX posted
  memory writes can complete even after I2S reset and DMA disable. Neither path
  is a silicon quiescence guarantee.

`scripts/ws53_mdma_i2s_test.py` checks 160 width/burst/channel combinations with
full-width payloads, 15 short tails, four odd-sample sequences, 12 posted-write/
error cases, clock pauses, FIFO errors, separate IRQs and 14 invalid diagnostics.
It is part of `scripts/test-ws53.py`; existing I2S PIO and other DMA tests remain.
The model emits a dedicated assumption notice. These tests move actual words
through memory, FIFOs, a serializer and the data-dependent virtual peer; they
do not certify physical audio timing, packing, caches or control-core ownership.
The integration repository now has an experimental Rust I2S DMA driver and
669 actual RAM scenarios; those remain model validation, not silicon acceptance.

## Shared-security TRNG test profile (not entropy)

The source map is SDK `163d9150` / hisi-registers `514615b`: FIFO_DATA at
`0x52024100`, FIFO_READY at `0x52024104` and halfword ring control at
`0x52001600`. There is no documented IRQ. The vendor application supplies
control-core random buffers, so a Rust token is not a complete service handoff.

`ws53-trng.assume-profile=on` explicitly enables a deterministic test generator.
Its nonzero `test-seed` defaults to 1. It uses xorshift32, not host randomness,
hardware entropy or a cryptographically secure source. It must never supply
production keys. A diagnostic records the assumption whenever enabled.
Without the option, enabling the ring is diagnosed and produces no data.

Profile assumptions:

- Eight-word FIFO, two words generated together after 200us startup, then every
  10us. The startup wait comes from SDK software; physical readiness timing,
  depth and pair admission are not proven. A full FIFO pauses production without
  advancing the sequence or overwriting words; one free slot is insufficient.
- READY and DONE mean two queued words. Status reads do not consume them.
  Each aligned 32-bit DATA read pops exactly one word, so the second read of an
  admitted pair remains legal when only one word is left. Underflow is diagnosed.
- Halfword ring writes preserve all 16 written bits. Only an enable transition
  starts/stops production; disabling discards queued data and cancels the timer.
  Re-enabling waits again and continues the deterministic sequence. Model reset
  clears the ring/queue/counters and restores the initial seed. These are model
  reset choices, not measured silicon reset values.
- External `clock` pauses time remaining and hides readiness while off; queued
  words persist. Source stall pauses production but leaves queued data readable.
- GPIO `fault` inputs 0–6 are source stall, missing READY, missing DONE, zero
  pairs, all-ones pairs, equal candidate/comparison and alternating ready/not-ready
  status reads. Pair faults apply during production (zero > ones > equal if
  combined); flags affect observation, not queue consumption. External clock
  and fault levels persist across model reset.
- Wrong widths, writes to FIFO/status, optional FRO registers, DATA_ST and unknown
  MMIO produce diagnostics. No catch-all success, security initialization, IRQ
  or control-core firmware service is simulated.

`scripts/ws53_trng_test.py` checks 512 deterministic words across four seeds,
seed replay, capacity/partial pairs, timing boundaries, pause/resume, ring
disable/re-enable, faults/reset and 19 rejected access diagnostics. It is part
of the complete WS53 model suite. The integration RAM fixture checks actual
Rust/PAC filtering, partial byte fills, timeout, service release and recovery;
its synthetic handoff is not proof of physical ownership or entropy quality.

## eFuse immutable read-service profile

SDK `163d9150` / hisi-registers `8d50f1e` establish one 256-byte logical region:
each aligned 32-bit DATA slot at `0x57024800 + (byte/2)*4` holds two bytes in
its low16. The model deliberately poisons upper16 with 0xdead to expose packed
word/byte-addressing mistakes. It does not load real device identities or secrets.

`ws53-efuse.assume-profile=on` permits the read service. The synthetic contents
are immutable bytes `(logical_index*73 + test-byte-key) & 255`; the key property
defaults to 19 and rejects values above255. Reset and all guest writes leave
these bytes unchanged. No default claim is made about physical OTP contents.

Explicit assumptions and controls:

- External `boot-done` starts low; test setup must assert it. `clock` starts high.
  BOOT0_DONE is visible only with the profile, clock and boot input present and
  no injected loss. This is a synthetic readiness contract, not a boot/ROM model.
- Software configures the word-wide timing register at +0x34, including its
  opaque upper24, and writes only read-mode low16=0x5a5a at +0x30. The entire
  written word is retained, allowing upper16 preservation to be verified.
  A usable command requires timing low8=0x1f and readiness. It admits exactly
  one DATA word; this is an explicit model assumption based on the SDK's command
  before each byte, not proof of silicon command-completion or admission rules.
- Data access without that command/profile is diagnosed. Status/config reads
  do not consume admission. Clock/boot/fault/timing changes invalidate it.
- `fault` inputs0–3 inject missing readiness, mode readback0x1234, timing readback7
  and mode corruption to0x1234 when a read command is issued. These are error
  conditions, not programming modes. QOM `fail-after-read=N` makes boot readiness
  disappear immediately after successful DATA readN, to test rejection of that
  candidate and reporting only the earlier successful prefix. Zero disables it;
  asserting `boot-done` again clears the latched loss.
- Model reset clears timing/mode/admission and counters; synthetic contents,
  externally set boot/clock/fault levels and the failure-injection threshold
  persist. These choices are not claimed as physical reset values.
- OTP DATA writes, AVDD, programming modes, the system programming switch,
  wrong widths/unaligned operations and unknown accesses are explicitly rejected.
  The switch region is mapped only to diagnose rejection, not to simulate an
  enabled programming path. There is no IRQ, power sequencer or provisioning API.

`scripts/ws53_efuse_test.py` covers three complete 256-byte images, stride/upper
bits, read admission, boot/clock/mode/timing faults, post-read loss/recovery,
immutable data after programming attempts and reset, and rejected diagnostics.
It runs in the full WS53 model suite. The integration repository's
`efuse_control` RAM fixture and `test-ws53-efuse.py` additionally execute actual
Rust/PAC reads in 111 cases: three image keys, full-region and boundary reads,
20 failed-prefix positions per key, configuration/command/boot/clock faults,
empty/invalid ranges and recovery. They check guarded output, exact access counts
and upper control bits; no programming path is exposed by that HAL. Shared boot/
security/control-core ownership, identity schemas and physical read timing remain
unverified; no real eFuse was accessed or programmed.

### PWM0P pad routing and explicit counter frequency (2026-10-23)

PWM channel0 now drives the pinctrl resolver at documented PWM0P routes:
MGPIO10/mode4, SDK47 (MGPIO21/SGPIO2)/mode1, AGPIO2/mode2. Input-buffer enable
remains independent of peripheral output; switching back to GPIO disconnects
PWM. Other alternate signals remain diagnosed rather than invented.

`-global ws53-pwm.assume-timing=on` still explicitly opts into the digital timing
model. `-global ws53-pwm.counter-hz=2000000` selects the effective rate observed
in A-board bounded RAM takeover: divider16, period200, about100us. The default
32000000 retains the previous SDK-reported assumption for existing tests.
Only these two profiles are accepted; neither models the unknown upstream
clock tree. No hardware claim is made for32MHz, all channels, finite/IRQ timing,
analog drive, or the two untested physical aliases.

`scripts/ws53_pwm_pad_test.py` and the full WS53 suite test both profiles,
25/50/75% and constant duty endpoints, all three source-defined aliases,
input-buffer independence, GPIO disconnection, stop-low and reset. Four invalid
frequencies are rejected at realize. The integration `pwm0-smoke` fixture uses
2MHz explicitly for its same-ELF comparison; its first160kHz expectation failed
on silicon and remains in the evidence. A match with the measured profile does
not establish the upstream clock handoff or fix the missing clock-tree support.

### PWM local divisor qualification (2026-10-23 follow-up)

The measured profile (`counter-hz=2000000`) now models local divisors1/2/4/8/16.
The property denotes the reference counter rate **at divisor16**. In this profile
an effective32MHz input divided by the latched field gives counter rates32/16/8/4/2MHz;
period200 therefore produces160/80/40/20/10kHz. The original SDK32MHz-counter
profile remains restricted to divisor16; it does not extrapolate a512MHz input
or authorize other divider encodings. Source identity and clock-tree switching
are still unmodeled.

`pwm_divider_test` checks the five waveforms and rejects encodings0/3/31. The
existing LOAD-edge behavior remains an explicit model assumption outside the
reviewed clear/write/set sequence. Integration `pwm0-clock-probe` uses the real
HAL `ClockDivider` API, verifies that a running group rejects changes, restores
Div16, stops and releases its pad. Three repeated same-ELF A-board runs match
QEMU and the independently sampled DSLogic waveforms. This qualifies the local
ratio in that RAM takeover, not IRQ/finite behavior, live glitch-free transitions,
all hardware channels or production cross-core clock ownership.

### PWM asynchronous START and initial load notification (2026-10-24)

A-board channel0/group0 at effective2MHz exposed two bugs hidden by the earlier
synchronous model: immediate EN-clear lost the initial enabled configuration;
after adding a10us guard, interpreting the initial load flag as block completion
truncated the first high to19us. The corrected HAL guards START, acknowledges
the initial load notification, then stages EN=0 with the SDK1us interval and
deferred START. Three identical-ELF board captures produced exact8/16 pulses,
100us periods,50% duty and quiet tails, with matching strict QEMU UART output.

`-global ws53-pwm.start-latch-ns=1000` opts into asynchronous acceptance.
This1us latency is an explicit regression-test assumption, not a measured
hardware constant. At acceptance the model samples live configuration;
early shadow changes are visible. `clear=immediate` in the older notice means
immediate at acceptance. Default0 retains the synchronous assumption for
existing tests. Finite initial loads assert the configuration flag in both
profiles. Continuous initial notification and exact counter boundaries remain
unqualified. The follow-up finite-counter observations below replace the
former completed-period readback model for pulse blocks; the raw counter
alone still cannot establish the elapsed number of repeated blocks.

`scripts/ws53_pwm_start_test.py` checks early EN overwrite, initial flag before
completion, exact8/16 digital pulses, deferred stop, W1C, reset of pending
START, invalid latency properties, and explicit diagnostics for overlapping
START or acceptance with a gated clock. The shared Rust finite fixture enables
the delayed profile plus icount shift3,align=off,sleep=on. IRQ72 physical
delivery, other channels, exact latency and arbitrary short-block counts remain
unverified. Long preemption can emit extras before software can observe them.

### Finite PWM counter readback (2026-10-24 follow-up)

Eighteen TCXO-timestamped A-board samples over8/16-pulse bursts establish a
1-based active pulse index: at40/90/140/240/440/740us, reads1/1/2/3/5/8; after
deferred stop at800/1600us, reads0. Simultaneous DSLogic confirms the pulse
counts and100us periods. Internal model accounting still counts completed
periods; finite/pulse-block PW_COUNT readback now adds1 while running.
`ws53_pwm_start_test.py` checks each high/low half-period index and stopped0.

The shared Rust `pwm0-finite-counter-probe` fixture uses TCXO-relative sample
times and no printing until collection ends. Integration `--mode pwm-counter`
requires the complete expected transcript,18 exact count/flag pairs, and each
sample within its designated20us window. Physical and virtual timestamps
need not be equal; both must satisfy those bounds. Missing/extra lines, stale
zero-based counts, incorrect flags and time-window violations are rejected.
This qualifies the measured finite case, not continuous mode, repeating-block
boundaries, overflow, different offset/polarity or other physical channels.

### PWM IRQ72 retained pending (2026-10-24)

A-board measurements distinguish the controller pending bit from peripheral
status: the initial configuration event sets LOCIPD72 while local delivery is
masked. Acknowledging PWM_PERIODLOAD_FLAG0 clears the peripheral flag but
leaves LOCIPD72=1. Unmasking therefore causes one empty-status ISR before the
later finite-completion ISR. Peripheral event masking, separately, retains the
raw flag without producing an IRQ until the event output is unmasked.

The shared controller now supports per-machine selection of latched inputs.
WS53 selects IRQ72 only; WS63/BS2X and unqualified WS53 sources retain their
previous profile. Peripheral deassertion no longer clears that selected
pending bit. This does not claim silicon trap-entry auto-ack or persistent
high-level reassertion semantics; those parts of the CPU profile still need
separate evidence. Reset remains the CPU's pending-state reset.

`ws53_irq_latch_test.py` injects IRQ71/72 high then low with guest delivery
disabled and executes actual CPU CSR instructions. LOCIPD1 must contain only
IRQ72; clearing IRQ71 preserves it and LOCIPCLR72 clears it. The shared Rust
`pwm0-finite-irq-probe` runs real ISR72, records entry versus nonempty event
counts, checks peripheral-mask retention, reads pending before local unmask,
and checks ACK plus rearm with simultaneous8/16-pulse physical capture. The
old model failed the same ELF with missing retained pending/empty entry.

ISR clients must act on returned event bits, not count every ISR entry as a
completed pulse block. Whole-group pending is deliberately preserved during
HAL transactions; blindly clearing it could lose another channel's event.
IRQ71 abnormal configuration was qualified subsequently as described below;
other physical PWM channels remain unqualified.

### PWM IRQ71 persistent errors and recovery (2026-10-24)

The A-board RAM `pwm0-abnormal-probe` injects channel0 duty100/period20
and overlapping G0/G1 selection, before routing the output pad. W1C alone
reasserts IRQ71: the first diagnostic reached its8-entry safety cap per case.
Masking abnormal delivery before ACK yields exactly one entry per case. The
count error additionally sets channel0's configuration-loaded flag; the group
conflict does not. Correcting the configuration, committing START, and then
clearing status recovers normal8/16-pulse output on MIO21/D11.

Three same-ELF runs (`88e07ecd54fad15216f9aee32bee621ea76e7606bfe5f916773662b076ff95e3`)
have exact UART parity and independently captured physical pulse counts.
See hisi-registers `evidence/ws53-pwm-irq71-hil.json` and integration
`docs/plan/evidence/ws53-pwm-fault-hil-2026-10-24/summary.json`.

The model separates active errors from sticky status: W1C reasserts active
errors at MMIO boundaries; corrected START removes the active condition, then
W1C clears sticky status. The precise reassertion delay and whether shadow
writes alone suffice are unmeasured; this is a conservative recovery profile.
No invalid waveform is claimed: invalid groups still suppress modeled output
atomically, an earlier assumption unqualified by this single-channel test.
Reset clears both active and sticky state. Tests exercise masking, W1C during
fault, repair, unmask without retrigger, and existing normal output regression.
