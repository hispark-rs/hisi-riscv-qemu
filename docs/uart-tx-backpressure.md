# Shared WS63/BS2X UART TX backpressure profile

`ws63-uart.tx-fifo-size` selects a synthetic FIFO capacity (1, or a power of two
from 16 through 2048); default 1 keeps immediate chardev output. With FIFO enable
clear the effective capacity is 1. This does not assert physical FIFO depth.

The runtime QOM boolean `tx-stalled` holds queued bytes until the host clears it.
Locate the UART under `/machine/unattached` by its `chardev` property. This is an
explicit test fault, not a guest register, modem/CTS implementation or baud model.
Full writes produce a guest-error diagnostic and preserve the retained bytes.
FIFO status reflects empty/full; line status reflects immediate drain, with no
separate shift register. No TX interrupt or character timing is implemented.

TX reset and system reset discard queued output. System reset leaves the explicit
host stall in effect; only the host releases it. RX behavior/interrupts are unchanged.
The default unstalled path retains the previous instant output behavior.

`uv run --script scripts/ws63_uart_tx_test.py --qemu <binary>` covers all three
ports on WS63/BS20/BS21/BS21E/BS22, both capacities, ring wrap, full-write rejection,
ordered drain, TX/system reset and FIFO-disabled capacity. Parent integration
runs the actual Rust nonblocking trait with a virtual peer; physical qualification
and legacy async TX cancellation remain separate.
