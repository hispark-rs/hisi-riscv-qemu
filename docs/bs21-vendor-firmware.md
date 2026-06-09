# Running BS21/BS2X vendor firmware on `-M bs21`

Status: **loaderboot executes** (the first vendor boot stage runs its full init on
`-M bs21`). This documents the signed-image format, the BS21 ROM table, and the
remaining boundaries — the BS21 analog of the WS63 C-SDK-on-QEMU work.

## What works

`scripts/bs21-vendor-boot.sh <loaderboot_sign.bin>` boots the fbb_bs2x prebuilt
loaderboot (`src/interim_binary/bs21e/bin/boot_bin/loaderboot-bs21e-1100e/`):
it runs **~480 instructions** of real vendor code — relocates the boot-param block
to the DTCM, brings up PMU (`0x57004600`), and reaches its interrupt-driven
download-mode idle spin (`j .` @0x4298e) — **all standard RV32 + xlinx, zero
illegal-instruction traps**. The xlinx decoder (active for `-M bs21` via the `ws63`
CPU type) handles the vendor compiler's custom instructions transparently.

## Signed-image format (loaderboot)

Multi-section HiSilicon signed image:

| offset | section |
|--------|---------|
| `0x000` | image header — magic `0x4bd2f01e`, header-len `0x40` (@0x0c), `KeyAlg 0x2a13c812` (@0x18, = the sign cfg) + zero pad |
| `0x100` | code-info header — magic `0x4bd2f02d`; **code size** = u32 @ (code-info + `0x24`); then a hash block |
| tail   | the code itself (`code_size` bytes, ending at EOF) → for loaderboot: 0x5c20 bytes @ file `0x300` |

The mask ROM copies the code to ITCM and jumps to it. We instead load the
extracted code at **`0x40000`** (`APP_ITCM_ORIGIN`, "use itcm start addr load
loaderboot") and set the reset PC there.

**flashboot** uses the SAME two-header structure, only with different magics
(image header `0x4b1e3c1e` = its ImageId, code-info header `0x4b1e3c2d` @0x100,
size @ code-info+0x24 = 0x8ab0, code at file 0x300). `bs21-vendor-boot.sh` accepts
both magics, so it loads flashboot too. flashboot also links to **`0x40000`**: it
**runs** ~206 instrs (reset → ULP_AON clock config @0x5702c*** → a BSS/relocate
clear loop → its main code @0x40552), then reaches an early halt (`j .` @0x40120)
when it touches the **SFC** (Serial Flash Controller @ `0x90000000`: it writes
`0x509` to `0x90000210` and calls an SFC routine @0x4097a) — the SFC/flash is not
modelled, so the flash read returns garbage and flashboot stops. flashboot's source
(`flashboot_init` + `usb_download` + `upgrade_version_check`) confirms it reads
flash early. **SFC modelling is the next boundary** for flashboot to load the app
and print its banner.

## Memory-map fix (found by running firmware)

The loaderboot reset code copies the boot-param block to **`0x20000000`** — the
real BS21 DTCM (`APP_DTCM_ORIGIN`, len `0x10000`). `bs21.c` had wrongly placed
DTCM at `0xF0000`; loaderboot faulted writing to `0x20002d50`. Fixed: DTCM →
`0x20000000`, ITCM window → `0x80000..0x100000`. (M1 + WS63 qtests unaffected.)

## `bs21_rom_call` — IMPLEMENTED (patches/v10.0.0/0005)

The ROM-call interceptor is now in place (mirrors `ws63_rom_call`, dispatched by
the disjoint PC range). It emulates the secure-libc set the BS2X boot stages call:
`memset_s 0x3d80c`, `memcpy_s 0x3e07e`, `memmove_s 0x3e95c`, `sprintf_s 0x3ef18`,
`snprintf_s 0x3ef60`, `vsnprintf_s 0x3ef92` (the printf family reuses the
chip-neutral `ws63_vformat()`). **Validated**: a synthetic test that `jalr`s to
`0x3d80c` (memset_s) on `-M bs21` is intercepted, the buffer is correctly filled,
and execution resumes at `ra` — serial prints `XA` (X = UART, A = the memset'd
byte). WS63 unchanged (5/5 qtests + M1 still pass). systick/tcxo/SFC/watchdog ROM
APIs are not mapped yet (their BS21 addresses differ) — they fall through to the
success stub. `cpu_helper.c` is version-volatile, so 0005 is on v10.0.0 (the
build.sh default); other QEMU versions need it rebased.

> **Note on loaderboot**: loaderboot is self-contained and makes **zero** ROM
> calls (it never executes below 0x40000), so `bs21_rom_call` does not change its
> behaviour — it reaches its download-mode idle spin either way. `bs21_rom_call`
> serves the later stages (flashboot/app), which call secure-libc heavily.

## BS21 ROM table (source for `bs21_rom_call`)

Source: `fbb_bs2x/src/drivers/chips/bs2x/rom/rom_config/acore/{acore_rom_n1200,
romboot,remote_lib_boot}.sym` (the BS21 `acore.sym` equivalents) + `librom_callback.a`.
The ROM region is **`~0x10000..0x42000`** — **disjoint from WS63's `0x109000..0x14C000`**,
so the interceptor can dispatch by PC range without a machine flag.

Secure-libc (the most-called by later stages), from `acore_rom_n1200.sym`:

| function | BS21 addr | function | BS21 addr |
|----------|-----------|----------|-----------|
| `memset_s`   | `0x3d80c` | `sprintf_s`   | `0x3ef18` |
| `memcpy_s`   | `0x3e07e` | `snprintf_s`  | `0x3ef60` |
| `memmove_s`  | `0x3e95c` | `vsnprintf_s` | `0x3ef92` |

SFC / boot (from `remote_lib_boot.sym`): `uapi_sfc_init=0x1037a`,
`uapi_sfc_init_rom=0x1044a`, `hal_sfc_init=0x1097c`, … (romboot text `0x102ec..0x11e90`).

To intercept, mirror WS63's `ws63_rom_call` (patches/<tag>/0001): when
`env->pc` is in the BS21 ROM range, emulate the function in host C (args in
a0–a3, result in a0) and resume at `ra`.

## Remaining boundaries (the deferred connectivity work)

1. ~~BS21 ROM-call table~~ — DONE (`bs21_rom_call`, patches 0005; see above).
2. ~~flashboot image format~~ — DONE (cracked; `bs21-vendor-boot.sh` runs it).
3. ~~SFC reg model~~ — DONE. The `ws63-sfc` v150 model (RDID→JEDEC ID) is mapped at
   `0x90000000` in bs21.c; flashboot's flash-ID init now succeeds (it runs 9 instrs
   further). M1 + WS63 unaffected.
4. ~~Flash CONTENT — partition table~~ — DONE. flashboot reads the partition table
   via **XIP at flash1 `0x90100000`** (its helper @0x406b4 just does `lui a0,0x90100;
   ret`) and checks the first word against magic **`0x4b87a52d`**. bs21.c now maps a
   `bs21.flash1` RAM region at 0x90100000; loading the prebuilt `partition.bin`
   (`src/interim_binary/bs21e/bin/partition/.../partition.bin`, magic at offset 0)
   there gets flashboot **past the magic** and **~940 instrs** (4x) deep into its
   main path (partition parsing) before a new idle spin @0x4293a. Reproduce:
   `bs21-vendor-boot.sh flashboot_sign_a.bin 5 0x40000 partition.bin`.
5. ~~Full flash image~~ — DONE. `bs21-build-flash.sh` unpacks the fbb_bs2x
   `bs21e_all.fwpkg` (loaderboot/partition/flashboot_a+b/**application**/nv) and lays
   each out at its partition flash offset (app @0x15000 → XIP 0x90115000); the boot
   script chunk-loads it at 0x90100000 (the generic loader caps a single raw load at
   ~0x10000). flashboot loads with the full flash present.
6. **The 0x4293a halt is a CRASH, not a boot-mode decision — it is the absent
   BS21 mask-ROM** (cracked 2026-06-09). Earlier notes guessed a "boot-reason check"
   at 0x41730; that was wrong — it came from objdump **misdecoding xlinx**. The real
   mechanism (decoded with the vendor xlinx-aware objdump, see below):

   - The spin @0x4293a is flashboot's **panic tail**: `irq_lock()` → record reason
     `0xdeadbeaf` into DTCM @0x2000ffe8 (`0x44f1c`) → clear bit0 of
     `BOOT_PORTING_RESET_REG` 0x57004600 (`0x42916`) → `j .`. It is reached from
     flashboot's **exception handler** (`mtvec = 0x47bbc`), which printf-dumps
     `exception:/uwExcType=/mepc=/mstatus=/mtval=/mcause=/ra=/sp=…` (strings @0x4871c+)
     via the log fn `0x43b40`. So flashboot **trapped**, then panicked.
   - **First trap: `mcause=0x2` (illegal instr), `mepc=0x0`.** flashboot validates the
     mask-ROM signature: `0x43c8a: lui a5,0x10; lw a4,32(a5)` reads `*(0x10020)` and
     `bne a4, 0xd4818193, 0x4403a`. The ROM region (0x10000–0x40000) is **zeroed RAM**
     in `-M bs21` (no mask-ROM dump exists), so `*(0x10020)=0 ≠ 0xd4818193` → it tail-
     `ret`s at 0x4403a with **ra=0** → jumps to PC 0 → illegal-instruction trap.
   - **Proof:** inject the magic — `-device loader,file=<0xd4818193>,addr=0x10020` —
     and flashboot **stops branching to the crash** (0 hits on 0x4403a), reaches the
     magic-OK path 0x43c98, and runs further (1260 vs 723 insns) before a **second**
     crash (`mcause=0x5` load-access-fault @ `0x570004a0`). So flashboot has **many**
     mask-ROM/peripheral dependencies; the magic is just the first.
   - **Conclusion:** like WS63, flashboot is tightly coupled to the silicon mask-ROM
     (signature + ROM data tables + ROM functions it tail-calls and registers
     callbacks into, e.g. ROM addr 0x1c200 at 0x43c9a). The SDK ships only
     `librom_callback.a` + `.sym` (no ROM image), so the path forward is **emulating
     the BS21 mask-ROM** (synthesize the signature/tables + extend `bs21_rom_call` for
     the functions it invokes) — the same scale as the WS63 ROM-on-QEMU work, i.e. the
     deferred connectivity-scale effort. This is NOT a single register to model.

   **Tooling note (reusable):** objdump misdecodes xlinx as `fld/fsd/.insn/illegal`.
   The fbb_ws63 vendor toolchain decodes it correctly — use
   `…/cc_riscv32_musl_105/cc_riscv32_musl_fp/bin/riscv32-linux-musl-objdump
   -b binary -m riscv:rv32 -D --adjust-vma=0x40000` on the extracted code. It shows the
   real `popret/push/pop/l.li/uxth/divu/...` (BS2X linx131 == WS63 xlinx, same ISA).

The infrastructure (CPU + xlinx + memory map + UART/GPIO + SFC + flash1 + the
disjoint-range ROM dispatch + bs21_rom_call) is in place; both loaderboot and flashboot
*run* and flashboot parses the partition table — the remaining work is the **BS21
mask-ROM emulation** so flashboot can pass its ROM-signature/ROM-call dependencies and
go on to load and start the application.
