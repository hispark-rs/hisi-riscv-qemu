#!/usr/bin/env bash
# Run a ws63-rs firmware ELF on the WS63 QEMU machine.
#
# Usage:
#   scripts/run.sh <firmware.elf> [extra qemu args...]
#   scripts/run.sh                      # defaults to the ws63-rs blinky ELF
#
# Env overrides:
#   QEMU_DIR   (default <repo>/qemu)        location of the built QEMU
#   WS63_RS    (default ../ws63-rs)         ws63-rs checkout (for the default ELF)
#   DEBUG=1    add `-d int,guest_errors -D qemu.log` for tracing
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_DIR="${QEMU_DIR:-$HERE/qemu}"
QEMU_BIN="${QEMU_BIN:-$QEMU_DIR/build/qemu-system-riscv32}"
WS63_RS="${WS63_RS:-$HERE/../ws63-rs}"

ELF="${1:-}"
if [ -z "$ELF" ]; then
    ELF="$WS63_RS/target/riscv32imfc-unknown-none-elf/release/blinky"
    echo "==> no ELF given, defaulting to $ELF"
fi
shift || true

[ -x "$QEMU_BIN" ] || { echo "QEMU not built: $QEMU_BIN (run scripts/build.sh)" >&2; exit 1; }
[ -f "$ELF" ]      || { echo "firmware ELF not found: $ELF" >&2; exit 1; }

ARGS=(-M ws63 -nographic -serial mon:stdio -kernel "$ELF")
if [ "${DEBUG:-0}" = "1" ]; then
    ARGS+=(-d int,guest_errors,unimp -D "$HERE/qemu.log")
    echo "==> tracing to $HERE/qemu.log"
fi

echo "==> $QEMU_BIN ${ARGS[*]} $*"
echo "    (exit QEMU with Ctrl-A X)"
exec "$QEMU_BIN" "${ARGS[@]}" "$@"
