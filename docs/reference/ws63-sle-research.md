# WS63 SLE/GLE software-contract research model

This is an opt-in, incomplete experiment tested on QEMU 10.0.0. Other QEMU
versions have not been qualified for this device. It is **not a working
SLE radio or an official register specification**. Existing WS63 runs keep the
old behavior unless `-device ws63-sle-research` is provided.

## Scope and evidence

The model provides a 64 KiB scratch MMIO window at `0x49000000` and 64 KiB EM RAM
at `0x49010000`. Those are experiment windows, not established silicon block
boundaries. Unmodeled MMIO uses scratch readback; this is explicitly an
assumption, not evidence that an address exists, is RW, or has no side effects.
Byte and halfword access to control registers has only scratch behavior.

| Address | Vendor software evidence | Model assumption |
|---|---|---|
| `0x49001410` | Firmware masks/unmasks ES bit5 | Bit5 gates modeled IRQ47 |
| `0x49001414` | Firmware tests ES status bit5 | Target timer sets bit5 |
| `0x4900141C` | ISR writes observed status, init writes all ones | Writing bit5 clears pending bit5; other sources absent |
| `0x490014FC` | GLE branch of `es_finetimer_prog_time` preserves upper2 bits | Write arms target; equal/past/wrap behavior not qualified |
| `0x49001500` | ROM sets bit31, polls it clear, returns low30 | Capture completes immediately; coarse tick defaults125us |
| `0x49001504` | ROM reads low7 after capture | Fine phase counts down in microseconds |
| `0x490015FC` | Queue init sets bits24/20, deinit clears20 | Scratch readback only; bit24 meaning unknown |
| `0x49001600` | Queue init writes low16=count*8; consumer reads bits26:19 | Scratch readback; no autonomous IRQ queue producer |

All reset zeros are deterministic **model initial conditions**, not recovered
chip reset values. Capture timing, coarse/fine phase, target scheduling, ACK
behavior and IRQ routing must ultimately be compared with hardware. No radio,
SED execution engine, EEQ consumer, encryption engine, PHY, packet success,
advertising, connection, IOB or IMB success is fabricated. Migration and hotplug
are unsupported. EM is real guest RAM,
but its contents are only modified by guest software or the test harness.

`trace-file=PATH` writes JSONL MMIO accesses with virtual nanoseconds, guest PC,
address, width and value; `E` marks a model event. GDB/qtest interventions are
also accesses: use the recorded byte ranges to distinguish executed firmware
from seeded inputs. EM accesses are not in this MMIO log.

## Build and run

Use an isolated QEMU source/build directory and the usual `scripts/build.sh`.
The new include file is copied by that script. For genuine ROM experiments,
after the regular patch application run:

```sh
python3 scripts/ws63_sle_strict_rom.py "$QEMU_DIR"
ninja -C "$QEMU_DIR/build" qemu-system-riscv32
python3 scripts/ws63_sle_model_test.py --qemu "$QEMU_DIR/build/qemu-system-riscv32"
python3 scripts/ws63_sle_vendor_probe.py \
  --qemu "$QEMU_DIR/build/qemu-system-riscv32" \
  --rom /path/to/captured-ws63-rom.bin --elf /path/to/iso-probe.elf \
  --out /path/to/research-results
```

The strict guard aborts on illegal instructions in the WS63 application-ROM
range when `WS63_SLE_STRICT_ROM` is set. Without it, the original QEMU hook can
silently turn an unsupported ROM instruction into a stub return, invalidating
firmware evidence. The vendor probe sets this environment variable. The normal
build script resets patched upstream sources, so reapply the guard after using
that script again. Missing boot-ROM code is not covered by this guard.

The supported ROM SHA-256 is
`22d92fbba1a7a7333a6fc4a8e1f38b791c06ec3b495e5dc8734bfb44575f39db`.
The initial function experiment uses the locally captured r2 diagnostic ELF
SHA-256 `95920853592b1ee92ce975e311fdd478719d2b1bfbeec7096b884cf70943a4d0`.
No firmware or proprietary SDK is added to
this repository. ROM function addresses are revision-specific. The test uses
synthetic stack, queue layout, memory offsets and scratch-register seeds; it
does **not** establish that boot initialization produced that state.

## First observations

Four genuine ROM functions returned under TCG with real instruction bytes:

* `0x136576 dts_gle_read_cur_clock`: set bit31, poll clear, read coarse clock.
* `0x1365A8 dts_gle_read_finecntclock`: same handshake then read fine low7.
* `0x13FBA0 gle_isr_queue_init`: count16 produces low16 `0x0080` at
  `0x49001600`, preserving upper16. Initializes consumer pointer/index/depth.
* `0x137C84 em_offset_init`: distinct synthetic layout offsets propagate to
  the expected MMIO registers. RMW masks are directly visible in the trace.

Four further cases run `0x13FC26 gle_isr_get_cur_isr_queue_head` with synthetic
queue entries: valid current item, skip invalid item, wrap slot15 to slot0, and
empty queue. The empty case returns a **non-NULL pointer to an invalid slot**
when consumer equals producer. Callers must test bit31, not just the pointer.
This is a firmware algorithm observation, not a simulated RF reception.

`em_offset_init` and the matching ELF symbols also identify the following
shared BLE configurations. They must not be relabeled SLE-only registers:

| MMIO | Software source | Write behavior |
|---|---|---|
| `0x49000274` | `g_em_ble_enc_plain_offset` at `0x180254` | Replace low20 with uint16 offset |
| `0x49001114` | `g_em_ble_fal_offset` at `0x180258` | Replace low20 with uint16 offset |
| `0x49001118` | `g_em_ble_fal_end` at `0x180256` | Replace low20 with uint16 end |
| `0x4900111C` | `g_em_ble_int_queue_offset` at `0x18025E` | Store uint16 offset using32-bit write |
| `0x49001130` | `g_em_ble_ral_offset` at `0x180262` | Replace low16 |
| `0x490012B0` | `g_em_ble_rx_desc_offset` at `0x180266` | Store uint16 offset using32-bit write |
| `0x490010C4` | No symbolic source | Clear bits15:8, then7:0; meaning unresolved |
| `0x49000430` | Constant initialization | Replace bits31:24 with `0x28`; meaning unresolved |

These are functional software mappings, not proofs of hardware field widths.
For low20 replacements from uint16 values, bits19:16 are explicitly cleared;
their purpose is still unknown. No hardware access/reset attributes are inferred.

The full r2 firmware initially reaches the scheduler with legacy ROM stubs and
then faults in the BT task (`mepc=0x2F874A`, `mtval=1`). Loading real ROM avoids
that particular stub path but reaches an unmodeled `0x40005018/1C` counter poll.
The SDK identifies this range as **SYSTICK**, not TRNG. Full boot and SLE
bring-up are therefore not yet passing. Function experiments isolate those
dependencies without claiming to have solved them.

The optional `assume-boot-systick=on` supplies a nominal32kHz counter at
`0x4000501C` (low32) / `0x40005018` (high16), matching ROM `rtc_read_once` at
`0x10AE02`. It is off by default, not a replacement for the clock/power tree.
It follows virtual time, including across device reset; it does not invent
progress on each read. A two-second real-ROM boot snapshot with this dependency
enabled stopped at `0x109B3C hal_sfc_regs_wait_config`, within the serial-flash
operation path (`ra=0x1095C4`). This is the next observed dependency, not proof
of a permanent deadlock or successful SLE initialization. The legacy SFC
model does not implement general flash command data transfers.

`scripts/ws63_sle_boot_probe.py` repeats bounded boot attempts and records PC,
registers, binary hashes, UART and MMIO traces. It accepts the same qemu/rom/elf/
out paths as the function probe, plus optional `--flash-dir`, `--boot-systick`
and `--seconds` (maximum30). It does not patch firmware, advance to a fabricated
success callback, or require a physical debug probe.

## Optional SFC data profile

`-global ws63-sfc.research-data=on` enables volatile command-data transfers
against the machine's existing 8 MiB XIP RAM window. It initializes that RAM
to erased bytes before QEMU loaders run. Host input files are never modified;
all programming and erase operations are confined to simulated memory.
The backing-window size is **not** a recovered flash geometry or a consequence
of the nominal JEDEC ID. This profile is off by default and does not change
the normal WS63/BS21 identification-only model.

The command register contract is based on the WS63 SDK SFC V150 definitions:
configuration at `0x48000300`, opcode at `+0x308`, address at `+0x30C`, sixteen
data words at `+0x400`; length is bits14:9 plus one, read is bit8, data enable
bit7, address enable bit3 and start bit0. The model accepts standard single-SPI
`03` reads and `02` programs, `06/04` write-enable control, `9F` ID,
`05/35/15` status reads, and `20/D8` sector/block erase. The command profile
does not include DMA, dual/quad transfers or vendor-specific status writes.

Immediate completion, no protection, status2/3 zero, 256-byte non-wrapping
program operations, 4/64 KiB erase sizes and partial data-word preservation
are explicit model conventions. Programming ANDs bytes with existing NOR
contents. Write enable is required and cleared after program/erase. Reset
clears controller state but retains flash. Unsupported commands/configurations,
out-of-range accesses and page-crossing programs terminate the research run
with a diagnostic; they are not silently acknowledged.

The inherited opt-in `WS63_SFC_TRACE=1` records the first100 commands. The data
regression test is `scripts/ws63_sfc_research_test.py --qemu BINARY`, covering
64-byte reads, partial reads, XIP coherence, write-enable, program/erase,
reset persistence and negative cases. It tests the declared model, not silicon.

`ws63_sle_boot_probe.py --sfc-data --boot-systick ...` enables both dependencies.
The probe checks ROM and ELF identity, records flash-input hashes and can stop
at `--break-at ADDRESS`. Its final stack/counter reads are GDB interventions;
the legacy TCXO model advances on reads. They are not guest execution evidence.
A ten-second attempt with the SFC profile did not reach `bt_init`; the captured
stack was in `hal_hash_lock` / `drv_cipher_hash_start` through TCXO delay calls.
This identifies another startup dependency, not a permanent-deadlock proof.

## BLE capture and shared queue function evidence

`assume-ble-clock=on` adds a separate, opt-in capture response at `0x490010A8`:
bit31 clears immediately, the low27 bits capture a nominal625us clock and
bits30:27 are preserved. The low10 bits at `0x490010AC` provide a synthetic
countdown phase. Period, phase and instantaneous completion are assumptions;
the ROM establishes only the request/poll/read pattern and extraction widths.
There is no autonomous BLE IRQ46 source or BLE target timer in this increment.

The vendor probe now runs19 scenarios: the earlier8, seven BLE clock/queue
counterparts, and four shared EEQ capacity cases. It verifies BLE queue init
at `0x13418C`, head selection at `0x134286`, clock reads at `0x136516/136548`,
and the EEQ helper at `0x139200`. BLE queue MMIO is `0x490012E0/12E4`, not the
GLE `0x490015FC/1600` pair. Both head readers can return non-null invalid items.
The BLE initialization case uses normal depth16 and urgent depth8, and observes
depth-minus-one and depth-minus-four groups at `0x49000448`.

The EEQ helper extracts6-bit capacity and7-bit occupancy, truncates their
difference to8 bits, then caps it at3. Synthetic capacity12/occupancy13 returns3
for both queues. This is executable software arithmetic; whether hardware can
present that input, and what calling invariant prevents it, remain unknown.
The probe now rejects mismatched ELF identity as well as mismatched ROM, since
its GP, callback addresses and global layout are fixed to the captured build.

For independently reconstructed C primitives, `--replacement-elf PATH --nm NM`
loads an additional experiment ELF and invokes its named `ws63_probe_*` bridge
functions instead of ROM entry points. The bridge must expose the clock,
queue-init/head, EM-offset and EEQ-allow functions plus `ws63_probe_status`.
The runner checks that status as well as the original synthetic-state
assertions, and records the replacement ELF hash and the original vendor PC.
This option does not alter ROM bytes or turn missing functions into successes.
The external reconstruction project provides the bridge and paired-trace
comparison. Its current19 scenarios agree on MMIO operation shapes and
non-clock data; clock-value differences reflect differing instruction time.

## EEQ submission probes

`scripts/ws63_eeq_vendor_probe.py` uses the same required qemu/rom/elf/out
arguments and optional replacement-elf/nm arguments. It executes six scenarios
for `evt_prog_push_eeq` at `0x139104`: empty/nonempty software lists and ring
wrap for normal and urgent queues. All use explicitly seeded environments,
SED references, queue storage and log-mask state. It checks the six-byte entry,
neighboring sentinel bytes, head/tail/count, next links, producer and all four
MMIO accesses at `0x49000444`. There is no autonomous EEQ consumer.

This function invokes ROM `memcpy_s`, whose ITCM trampoline jumps to linked
SRAM `memcpy`. Unlike earlier leaf probes, it needs executable relocations that
startup normally performs. The fixture extracts the original nonidentity
executable PT_LOAD segments from the pinned ELF, loads their exact bytes at
their VMAs and verifies the trampoline/memcpy entry bytes before running.
Report metadata contains file offsets, VMAs, LMAs, lengths and hashes. These
are synthetic setup actions, not boot evidence or function substitutions.
The initial unrelocated and SRAM-only experiments timed out at PC0; their
failure snapshots remain local. Restoring both ITCM and SRAM permits all six
scenarios to execute actual vendor code and return.

The optional replacement bridge is `ws63_probe_eeq_push(task,queue,urgent)` and
must update `ws63_probe_status`. This fixture adapter uses the fixed r2 test
layout; the reconstructed library itself need not adopt that private ABI.
Direct volatile accesses in the bridge are QEMU-only and do not establish the
silicon cache/barrier policy. Paired runs compare final EM/software state and
MMIO sequences, not every RAM access ordering or concurrent hardware behavior.


### Linked EEQ event association

`ws63_eeq_match_probe.py` accepts the same `--qemu`, `--rom`, `--elf`, `--out`
and optional `--replacement-elf`/`--nm` arguments. It executes linked patch
`0x14EBC2` and original ROM list routines with exact executable relocations
from the pinned ELF. Twenty-eight cases cover normal/urgent lists, locate/fetch,
exact-match priority, label fallback, clock fallback, low-byte alias, singleton,
miss and empty lists. Event code9 is supplied synthetically. The harness checks
return pointer, all next links, head/tail/count, copied protocol byte, unchanged
other queue and unchanged IRQ entry. No controller MMIO is expected here.

The observed association order is clock-low8 + label-low8, then full16 label,
then clock-low8. A label0x103 can match the first pass for IRQ label3 but not the
second. IRQ bit24 selects the list; bytes1/2 supply the clock/label. Lookup
retains list membership; fetch unlinks and clears next. These are software
contracts only: there is no hardware IRQ producer, actual RF completion or
unmatched RX-descriptor cleanup in this fixture. Reconstructed code uses its
own ABI through a QEMU-only bridge, not the vendor SDK structure ABI.


### RX descriptor release probes

`ws63_rx_release_probe.py` uses the same explicit input and replacement options.
It calls real BLE/GLE descriptor-free ROM functions for 22 cases: both protocols,
all 16-buffer group boundaries, invalid IDs 64/255, and bit15 unset. The fixture
checks the whole synthetic descriptor pool plus adjacent sentinels, the one
32-bit release command when applicable, and an unchanged software RX cursor.
It uses original executable relocations for the disabled-logging path.

The actual software order is halfword+2 read, byte+34 read, shared buffer command,
halfword+2 re-read, bit15 clear. Buffer commands write bit16+(id modulo16) to
`0x490004AC + 4*(id/16)` for IDs 0..63. No register RMW is performed. The model
records the write but does not implement a physical buffer-owner transition.
For invalid IDs the ROM sends no command and still clears bit15. The replacement
intentionally rejects those 4 fixtures and retains the descriptor; this is
reported separately from the 18 matching outcomes, not hidden as equivalence.
The unset-bit case retains memory in both; the new API exposes a status error.

This suite proves neither actual packet reception nor silicon ownership,
access attributes, readback or command-effect timing. All raw results stay in
local evidence; this repository contains only the source harness and guidance.


### Linked IRQ service control flow

`ws63_irq_dispatch_probe.py` executes fixed BLE/GLE ISR entry points with the
usual explicit ROM/ELF/QEMU/output arguments and optional replacement ELF/nm.
Twelve cases cover single, batch, wrap, skip, empty and full queues for both.
Selected callback-table entries explicitly point to the original ROM slot
handler (a no-op). GDB stops at that handler, platform IRQ clear and notifier,
records arguments/slot state, then executes the original instructions onward.
No breakpoint path fabricates a return or replaces a callback implementation.

The fixture constructs g_hwiOps with the original HalIrqClear callback and sets
the HCI queue pointer to zero. Thus CSR clear executes, notification is attempted,
and the real notifier takes its absent-queue branch; no message is delivered.
The replacement bridge reuses these original platform helpers while the IRQ
core decisions/MMIO run reconstructed C. This is deliberately a mixed platform
fixture, not a standalone replacement platform port or real event processing.

For these status-zero scenarios, ACK precedes clear/capture, both protocols
capture GLE clock, callback sees VALID, release preserves other word bits and
the second word, and the next entry is traversed without a fresh producer read.
Nonzero-status retry/start fallback, asynchronous arrivals, real EEQ callbacks
and OS delivery remain outside this original-code runtime suite.


### Generic event lifecycle and unmatched RX cleanup

`ws63_event_lifecycle_probe.py` exercises10 matched completion/RX cases using
real LOS_MemInit/Alloc/Free and dts_free. The callback is explicitly the original
ROM no-op slot handler. GDB observes callback arguments,12-byte payload, queue
ownership and subsequent free; reallocation checks address reuse after completion
and non-reuse while RX retains the task. Deferred link-task GC and mode-specific
callback processing are excluded. The replacement uses new C metadata through a
bridge but retains original allocator/callback hooks as an explicit boundary.

This fixture restores executable AND initialized-data PT_LOAD contents at VMA.
An executable-only attempt failed in __clzsi2 because its GOT word was missing.
The original GOT value is now verified; segment flags and hashes are recorded.
No helper/allocator replacement or invented return is used. Boot remains unproven.

`ws63_rx_orphan_probe.py` adds24 cases for unmatched event10: last-protocol tags1,
2,0,3, both urgent settings, cursor0/wrap3, bit15 set/unset and invalid buffer64.
The last protocol selects the RX pool; successful cleanup advances a numeric
cursor, not the descriptor NEXT link. Twenty outcomes match. Four deliberately
differ: invalid buffer IDs retain descriptor/cursor in the replacement but the
ROM clears/advances without a buffer-release write. No hardware ownership claim
is inferred. Restoring data also enables logging defaults; the fixture calls the
actual log_set_local_log_level(0) API (LOG_LEVEL_NONE) before executing the original
filter path, because the logger backend is not initialized. This is a documented
configuration precondition, not a patched logging function. Both initial failures
and subsequent successful evidence remain local.
