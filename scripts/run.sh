#!/usr/bin/env bash
# Run a firmware ELF on the WS63 QEMU machine.
#
# Usage:
#   scripts/run.sh <firmware.elf> [extra qemu args...]
#   scripts/run.sh                      # defaults to a prebuilt C SDK fixture
#
# Env overrides:
#   QEMU_DIR      (default <repo>/qemu)     location of the built QEMU
#   DEFAULT_ELF   (default tests/csdk/dma.elf)    default firmware when no ELF is given
#   DEBUG=1       add `-d int,guest_errors -D qemu.log` for tracing
#   ICOUNT=1      deterministic instruction-counted timing (`-icount shift=N`):
#                 reproducible run-to-run, IPC=1 at ~250 MHz. NOT cycle-accurate.
#   ICOUNT_SHIFT  override the shift (default 2 -> 4 ns/insn ~ 250 MHz; 3 -> 125 MHz)
#   NV=1          back the flash XIP window with the partition table + NV images
#                 (tests/csdk/flash/) so the C SDK's partition/NV reads succeed
#                 (a -kernel boot otherwise skips flashboot and leaves flash empty)
#   SEMIHOST=1    enable RISC-V semihosting (-semihosting): firmware can call
#                 SYS_EXIT to set the QEMU process exit code (pass/fail for CI
#                 without UART scraping) and SYS_WRITE0 to print to the console.
#                 See the ws63-rs semihost_selftest example.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_DIR="${QEMU_DIR:-$HERE/qemu}"
QEMU_BIN="${QEMU_BIN:-$QEMU_DIR/build/qemu-system-riscv32}"
DEFAULT_ELF="${DEFAULT_ELF:-$HERE/tests/csdk/dma.elf}"

ELF="${1:-}"
if [ -z "$ELF" ]; then
    ELF="$DEFAULT_ELF"
    echo "==> no ELF given, defaulting to $ELF"
fi
shift || true

[ -x "$QEMU_BIN" ] || { echo "QEMU not built: $QEMU_BIN (run scripts/build.sh)" >&2; exit 1; }
[ -f "$ELF" ]      || { echo "firmware ELF not found: $ELF" >&2; exit 1; }

ARGS=(-M ws63 -nographic -serial mon:stdio -kernel "$ELF")
if [ "${ICOUNT:-0}" != "0" ]; then
    SHIFT="${ICOUNT_SHIFT:-2}"
    ARGS+=(-icount "shift=$SHIFT")
    echo "==> deterministic timing: -icount shift=$SHIFT (~$((1000 / (1 << SHIFT))) MHz, IPC=1; not cycle-accurate)"
fi
if [ "${NV:-0}" != "0" ]; then
    FLASH_DIR="${FLASH_DIR:-$HERE/tests/csdk/flash}"
    MAN="$FLASH_DIR/manifest.txt"
    if [ -f "$MAN" ]; then
        while IFS='|' read -r file addr; do
            case "${file// /}" in ''|\#*) continue;; esac
            file="$(echo "$file" | xargs)"; addr="$(echo "$addr" | xargs)"
            [ -f "$FLASH_DIR/$file" ] || continue
            ARGS+=(-device "loader,file=$FLASH_DIR/$file,addr=$addr")
            echo "==> flash overlay: $file @ $addr"
        done < "$MAN"
    else
        echo "==> NV=1 but no flash manifest at $MAN" >&2
    fi
fi
if [ "${SEMIHOST:-0}" != "0" ]; then
    ARGS+=(-semihosting)
    echo "==> semihosting enabled (firmware SYS_EXIT sets the QEMU exit code)"
fi
if [ "${DEBUG:-0}" = "1" ]; then
    ARGS+=(-d int,guest_errors,unimp -D "$HERE/qemu.log")
    echo "==> tracing to $HERE/qemu.log"
fi

echo "==> $QEMU_BIN ${ARGS[*]} $*"
echo "    (exit QEMU with Ctrl-A X)"
exec "$QEMU_BIN" "${ARGS[@]}" "$@"
