#!/usr/bin/env bash
# Smoke-test the WS63 QEMU machine against ws63-rs firmware.
#   - uart_hello: assert the banner prints over UART0 (custom UART device)
#   - blinky:     assert the firmware reaches the GPIO0 toggle loop (MMIO trace)
# Exit 0 = both pass.
#
# Env: QEMU_DIR (default <repo>/qemu), WS63_RS (default ../ws63-rs)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QEMU_DIR="${QEMU_DIR:-$HERE/qemu}"
QEMU_BIN="${QEMU_BIN:-$QEMU_DIR/build/qemu-system-riscv32}"
WS63_RS="${WS63_RS:-$HERE/../ws63-rs}"
TARGET_DIR="$WS63_RS/target/riscv32imfc-unknown-none-elf/release"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
[ -x "$QEMU_BIN" ] || { echo "FATAL: QEMU not built ($QEMU_BIN)"; exit 2; }

# ---- uart_hello: serial output ----
UART_ELF="$TARGET_DIR/uart_hello"
if [ -f "$UART_ELF" ]; then
    echo "==> uart_hello: expecting UART banner"
    timeout 5 "$QEMU_BIN" -M ws63 -nographic -serial mon:stdio \
        -kernel "$UART_ELF" >"$TMP/uart.out" 2>/dev/null
    if grep -q "Hello from WS63 on QEMU" "$TMP/uart.out"; then
        echo "    PASS: $(grep -m1 Hello "$TMP/uart.out")"
    else
        echo "    FAIL: banner not found. Got:"; head -5 "$TMP/uart.out" | sed 's/^/      /'
        fail=1
    fi
else
    echo "==> uart_hello: SKIP (build it: cargo build -p uart_hello --release)"
fi

# ---- blinky: GPIO toggle loop ----
BLINKY_ELF="$TARGET_DIR/blinky"
if [ -f "$BLINKY_ELF" ]; then
    echo "==> blinky: expecting GPIO0 (0x44028xxx) writes + no illegal-instruction traps"
    timeout 3 "$QEMU_BIN" -M ws63 -nographic -serial mon:stdio \
        -d int,unimp,guest_errors -D "$TMP/blinky.log" \
        -kernel "$BLINKY_ELF" >/dev/null 2>&1
    traps=$(grep -c illegal_instruction "$TMP/blinky.log" 2>/dev/null)
    gpio=$(grep -c '0x4028030\|0x4028034\|0x4028004' "$TMP/blinky.log" 2>/dev/null)
    traps=${traps:-0}
    gpio=${gpio:-0}
    if [ "$traps" -eq 0 ] && [ "$gpio" -gt 0 ]; then
        echo "    PASS: $gpio GPIO0 accesses, 0 illegal-instruction traps"
    else
        echo "    FAIL: gpio_writes=$gpio illegal_traps=$traps"
        fail=1
    fi
else
    echo "==> blinky: SKIP (build it: cargo build -p blinky --release)"
fi

[ "$fail" -eq 0 ] && echo "SMOKE TEST: PASS" || echo "SMOKE TEST: FAIL"
exit "$fail"
