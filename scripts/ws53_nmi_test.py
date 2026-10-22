#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 source-profile NMI aggregation tests, explicitly excluding CPU delivery."""
import argparse
import json
from ws53_gpio_test import running

BASE = 0x52000700
UNIMPLEMENTED_CPU = "ws53-nmi: unimplemented CPU NMI delivery (aggregation output only)"


def nmi_test(qemu):
    combinations = 0
    with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
        command("irq_intercept_out /machine/nmi pending")
        def read(off):
            return int(command(f"readw {BASE+off:#x}")[0], 0)
        def write(value):
            command(f"writew {BASE:#x} {value:#x}")
        def source(bit, high):
            command(f"set_irq_in /machine/nmi source {bit} {int(high)}")
        def check(raw, enable):
            assert read(0) == enable
            # Repeated reads must not acknowledge or consume the source.
            for _ in range(2):
                assert read(4) == raw and read(8) == raw & enable
            assert irq.get(0, False) == bool(raw & enable)
        check(0, 0)
        source(1, True)
        check(2, 0)
        write(2)
        check(2, 2)
        write(0)
        check(2, 0)
        write(2)
        check(2, 2)
        # Every possible set of the five evidenced inputs against every enable
        # mask. Hardware source latches remain the owning peripheral's concern.
        for raw in range(32):
            for bit in range(1, 6):
                source(bit, bool((raw << 1) & (1 << bit)))
            for enable in range(32):
                write(enable << 1)
                check(raw << 1, enable << 1)
                combinations += 1
        for bit in range(1, 6):
            source(bit, True)
        write(0x3e)
        raw = 0x3e
        for bit in range(1, 6):
            source(bit, False)
            raw &= ~(1 << bit)
            check(raw, 0x3e)  # no output drop until the final active input clears
        source(1, True)
        source(4, True)
        write(0x12)
        check(0x12, 0x12)
        reset()
        check(0x12, 0)  # model reset must not fabricate source deassertion
        write(2)
        check(0x12, 2)
        source(1, False)
        check(0x10, 2)
        write(0x10)
        check(0x10, 0x10)
        source(4, False)
        check(0, 0x10)
    assert diagnostics[0].strip() == UNIMPLEMENTED_CPU, diagnostics

    with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
        # The aggregate is not a normal local IRQ (neither cause12 nor ccore86).
        command("irq_intercept_in /machine/intc")
        command(f"writew {BASE:#x} 2")
        command("set_irq_in /machine/nmi source 1 1")
        assert int(command(f"readw {BASE+8:#x}")[0], 0) == 2
        assert not any(irq.values()), irq
    assert diagnostics[0].strip() == UNIMPLEMENTED_CPU, diagnostics

    invalid = [f"readb {BASE:#x}", f"writeb {BASE:#x} 0",
               f"readl {BASE:#x}", f"writel {BASE:#x} 0",
               f"readw {BASE+1:#x}", f"writew {BASE+1:#x} 0",
               f"readw {BASE+2:#x}", f"writew {BASE+2:#x} 0",
               f"readw {BASE+6:#x}", f"readw {BASE+10:#x}",
               f"writew {BASE+4:#x} 0", f"writew {BASE+8:#x} 0",
               f"writew {BASE:#x} 1", f"writew {BASE:#x} 0xffff",
               "set_irq_in /machine/nmi source 0 1",
               "set_irq_in /machine/nmi source 15 1"]
    with running(qemu, direct_gpio=False) as (command, irq, reset, diagnostics):
        command("irq_intercept_out /machine/nmi pending")
        command(f"writew {BASE:#x} 2")
        command("set_irq_in /machine/nmi source 1 1")
        for operation in invalid:
            command(operation)
            for off in (0, 4, 8):
                assert int(command(f"readw {BASE+off:#x}")[0], 0) == 2, operation
            assert irq[0], operation
    lines = diagnostics[0].splitlines()
    assert len(lines) == len(invalid)+1 and lines.count(UNIMPLEMENTED_CPU) == 1, diagnostics
    assert all(line.startswith("ws53-nmi: unimplemented") for line in lines), diagnostics
    return {"registers": 3, "access_bits": 16, "source_bits": [1,2,3,4,5],
            "enable_source_combinations": combinations, "invalid_operations_diagnosed": len(invalid),
            "checks": ["source/enable truth table", "reads have no clear side effect",
                       "mask preserves active raw input", "last-source deassertion",
                       "reset masks but preserves source levels", "reenable held source",
                       "no ordinary local IRQ route", "invalid accesses retain state",
                       "CPU delivery gap always diagnosed"],
            "status_model": "source-driven levels; peripheral latches outside this block",
            "cpu_nmi_delivery_modeled": False, "watchdog_source_modeled": False,
            "hardware_verified": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(nmi_test(parser.parse_args().qemu), indent=2))
