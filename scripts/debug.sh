#!/usr/bin/env bash
# Debug a hisi-riscv-rs firmware ELF: launch the WS63 QEMU with a gdbstub (halted at
# entry) and attach rust-gdb, which drives gdb-multiarch (it understands riscv:rv32)
# and auto-loads the Rust pretty-printers from the ws63 toolchain sysroot.
#
# Usage:
#   scripts/debug.sh <firmware.elf | example-name> [extra gdb args...]
#   scripts/debug.sh blinky
#   scripts/debug.sh blinky -ex 'break main' -ex 'continue'
#
# UART output goes to $SERIAL_LOG (default <repo>/debug-serial.log) so it doesn't
# fight gdb for the terminal — `tail -f` it in another window if you need it.
#
# Env overrides:
#   QEMU_DIR / QEMU_BIN / WS63_RS  (as in run.sh)
#   PORT            gdbstub TCP port (default 1234)
#   RUST_GDB        gdb binary rust-gdb drives (default gdb-multiarch; plain `gdb`
#                   usually can't do riscv:rv32). `apt-get install gdb-multiarch`.
#   GDB_TOOLCHAIN   rustup toolchain whose rust-gdb + printers to use (default ws63)
#   SERIAL_LOG      where to write UART output (default <repo>/debug-serial.log)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_DIR="${QEMU_DIR:-$HERE/qemu}"
QEMU_BIN="${QEMU_BIN:-$QEMU_DIR/build/qemu-system-riscv32}"
WS63_RS="${WS63_RS:-$HERE/../hisi-riscv-rs}"
PORT="${PORT:-1234}"
export RUST_GDB="${RUST_GDB:-gdb-multiarch}"
GDB_TOOLCHAIN="${GDB_TOOLCHAIN:-ws63}"
SERIAL_LOG="${SERIAL_LOG:-$HERE/debug-serial.log}"

ELF="${1:?usage: debug.sh <firmware.elf | example-name> [gdb args...]}"; shift || true
# Resolve a bare example name to its release ELF under hisi-riscv-rs.
if [ ! -f "$ELF" ]; then
    CAND="$WS63_RS/target/riscv32imfc-unknown-none-elf/release/$ELF"
    [ -f "$CAND" ] && ELF="$CAND"
fi
[ -x "$QEMU_BIN" ] || { echo "QEMU not built: $QEMU_BIN (scripts/build.sh)" >&2; exit 1; }
[ -f "$ELF" ]      || { echo "firmware ELF not found: $ELF" >&2; exit 1; }

# rust-gdb from the chosen toolchain (auto-loads the Rust printers from its sysroot).
RUST_GDB_BIN="$(rustup which --toolchain "$GDB_TOOLCHAIN" rust-gdb 2>/dev/null || true)"
[ -x "$RUST_GDB_BIN" ] || RUST_GDB_BIN="rust-gdb"
command -v "$RUST_GDB" >/dev/null 2>&1 || \
    echo "WARNING: '$RUST_GDB' not found — install it (apt-get install gdb-multiarch)" >&2

echo "==> QEMU gdbstub on :$PORT (halted at entry); UART → $SERIAL_LOG"
"$QEMU_BIN" -M ws63 -display none -monitor none -serial "file:$SERIAL_LOG" \
    -kernel "$ELF" -S -gdb "tcp::$PORT" &
QEMU_PID=$!
trap 'kill "$QEMU_PID" 2>/dev/null || true' EXIT INT TERM
# Wait for the gdbstub socket to open (up to ~3s).
for _ in $(seq 1 30); do
    (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && { exec 3>&- 3<&-; break; }
    sleep 0.1
done

echo "==> attaching $RUST_GDB_BIN (RUST_GDB=$RUST_GDB) → target remote :$PORT"
"$RUST_GDB_BIN" -ex "target remote :$PORT" "$ELF" "$@"
