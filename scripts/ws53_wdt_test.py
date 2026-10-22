#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 WDT explicit timing-assumption tests; not hardware timing or CPU reset proof."""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_nmi_test import UNIMPLEMENTED_CPU

BASE = 0x5702b000
NMI = 0x52000700
KEY = 0x5a5a5a5a
TICK = 31250
RESET = "ws53-wdt: unimplemented SoC reset topology/pulse (request only)"
UNKNOWN = "ws53-wdt: unimplemented counter timing without assume-timing profile"


def profile(low):
    return ("-global", "ws53-wdt.assume-timing=on", "-global", f"ws53-wdt.assumed-load-low-byte={low}")


def notice(low):
    return f"ws53-wdt: assumed timing low-byte={low} expiry=count+1 stage=reload clock=32000"


def access(command):
    def read(off):
        return int(command(f"readl {BASE+off:#x}")[0], 0)
    def write(off, value):
        command(f"writel {BASE+off:#x} {value:#x}")
    def sample():
        write(0x28, 1)
        assert read(0x28) == 2
        return read(0x14)
    def step(ns):
        command(f"clock_step {ns}")
    def input_(name, high):
        command(f"set_irq_in /machine/wdt {name} 0 {int(high)}")
    def start(load=0x100, control=0xbd):
        write(0, KEY)
        assert read(0) == 0
        read(0xc)
        write(0x10, control & ~1)
        write(4, load)
        write(8, KEY)
        read(0xc)
        write(0x10, control)
        write(8, KEY)
    return read, write, sample, step, input_, start


def wdt_test(qemu):
    invalid_property = subprocess.run([qemu, "-M", "ws53", "-accel", "qtest",
        "-display", "none", "-nodefaults", *profile(256)], capture_output=True,
        text=True, timeout=5)
    assert invalid_property.returncode != 0
    assert "assumed-load-low-byte must be 0..255" in invalid_property.stderr
    with running(qemu, False) as (command, irq, reset, diagnostics):
        read, write, _, step, _, _ = access(command)
        read(0xc)  # consuming read while protection is unknown
        write(0, KEY)
        read(0x14)  # no valid snapshot exists
        write(4, 0x100)
        write(8, KEY)
        write(0x10, 0xbd)
        write(0x28, 1)
        step(1_000_000_000)
        assert read(0x10) == read(0x18) == read(0x28) == 0
    assert diagnostics[0].splitlines() == [
        "ws53-wdt: unimplemented read +0xc size=4",
        "ws53-wdt: unimplemented read +0x14 size=4", *([UNKNOWN] * 3)], diagnostics

    # Test both extreme low-byte assumptions. This is a parameter sweep, not
    # evidence that either value describes the actual silicon.
    for low in (0, 255):
        with running(qemu, False, profile(low)) as (command, irq, reset, diagnostics):
            read, write, sample, step, input_, start = access(command)
            command("irq_intercept_out /machine/wdt reset-request")
            start()
            count = 256 + low
            period = (count + 1) * TICK
            assert sample() == count
            step(TICK // 2)
            input_("clock", False)
            write(0x28, 1)
            assert read(0x28) == 0
            step(10 * period)
            assert read(0x18) == 0 and not irq.get(0, False)
            input_("clock", True)
            assert read(0x28) == 2 and read(0x14) == count
            step(TICK - TICK // 2)
            assert sample() == count - 1  # fractional clock phase survived gating
            write(8, KEY)
            assert sample() == count
            step(2 * TICK)
            assert read(0x14) == count  # snapshot must not become a live counter
            assert sample() == count - 2
            input_("stall-snapshot", True)
            write(0x28, 1)
            step(TICK)
            assert read(0x28) == 0
            input_("stall-snapshot", False)
            assert read(0x28) == 2 and read(0x14) == count - 3
            write(8, KEY)
            step(period - 1)
            assert sample() == 0 and read(0x18) == 0
            step(1)
            assert read(0x18) == read(0x1c) == 1
            assert int(command(f"readw {NMI+4:#x}")[0], 0) == 2
            assert not irq.get(0, False) and sample() == count
            # Kick restarts the profile's first stage but does NOT clear pending.
            write(8, KEY)
            assert read(0x18) == 1
            step(period)
            assert not irq.get(0, False)
            assert read(0xc) == 0 and read(0x18) == 0
            # EOI alone does not restart the assumed stage sequence.
            step(period)
            assert irq[0] and read(0x18) == 1
            reset()
            assert read(0) == 1 and read(4) == read(0x10) == read(0x18) == read(0x28) == 0
            assert not irq[0]
            assert int(command(f"readw {NMI+4:#x}")[0], 0) == 0
            start()
            assert sample() == count
        assert diagnostics[0].splitlines() == [notice(low), RESET], diagnostics

    with running(qemu, False, profile(255)) as (command, irq, reset, diagnostics):
        read, write, sample, step, input_, start = access(command)
        command("irq_intercept_out /machine/wdt reset-request")
        # No reset enable: repeated expiries remain interrupt-only in this profile.
        start(control=0xb9)
        step(512 * TICK)
        assert read(0x18) == read(0x1c) == 1
        write(0xc, 1)  # an unevidenced write-clear must not consume pending
        command(f"readw {BASE+0xc:#x}")  # wrong-width EOI must not consume it either
        assert read(0x18) == read(0x1c) == 1
        write(0x10, 0xf9)
        assert read(0x18) == 1 and read(0x1c) == 0
        assert int(command(f"readw {NMI+4:#x}")[0], 0) == 0
        step(512 * TICK)
        assert not irq.get(0, False)
        read(0xc)
        write(0x10, 0xb9)
        # Enable the aggregator: actual WDT output must reach SDK source bit 1.
        command(f"writew {NMI:#x} 2")
        step(512 * TICK)
        assert int(command(f"readw {NMI+8:#x}")[0], 0) == 2
        read(0xc)
        assert int(command(f"readw {NMI+8:#x}")[0], 0) == 0
        step(3 * TICK)
        before = sample()
        write(0x10, 0xb8)
        step(1000 * TICK)
        assert sample() == before and read(0x18) == 0
        start(control=0xb9)
        assert sample() == 511
        input_("clock", False)
        input_("stall-snapshot", True)
        reset()
        write(0, KEY)
        write(0x28, 1)
        assert read(0x28) == 0
        input_("clock", True)
        assert read(0x28) == 0
        input_("stall-snapshot", False)
        assert read(0x28) == 2 and read(0x14) == 0
    assert diagnostics[0].splitlines() == [notice(255),
        "ws53-wdt: unimplemented write +0xc value=0x1 size=4",
        "ws53-wdt: unimplemented read +0xc size=2", UNIMPLEMENTED_CPU], diagnostics

    with running(qemu, False, profile(255)) as (command, irq, reset, diagnostics):
        read, write, sample, step, _, start = access(command)
        command("irq_intercept_out /machine/wdt reset-request")
        start(0xffffff00, 0x3d)  # first-expiry reset, maximal 32-bit counter
        assert sample() == 0xffffffff
        step((1 << 32) * TICK - 1)
        assert sample() == 0 and not irq.get(0, False)
        step(1)
        assert irq[0] and read(0x18) == 1
    assert diagnostics[0].splitlines() == [notice(255), RESET], diagnostics

    with running(qemu, False, profile(0)) as (command, irq, reset, diagnostics):
        read, write, sample, step, _, start = access(command)
        command("irq_intercept_out /machine/wdt reset-request")
        start(0, 0x3d)
        assert sample() == 0 and read(0x18) == 0
        step(TICK - 1)
        assert not irq.get(0, False)
        step(1)
        assert irq[0]
    assert diagnostics[0].splitlines() == [notice(0), RESET], diagnostics

    invalid = [f"readb {BASE:#x}", f"readw {BASE:#x}", f"readq {BASE:#x}",
               f"readl {BASE+1:#x}", f"readl {BASE+8:#x}",
               f"readl {BASE+0x20:#x}", f"readl {BASE+0x24:#x}", f"readl {BASE+0x2c:#x}",
               f"writeb {BASE:#x} 0", f"writew {BASE:#x} 0", f"writeq {BASE:#x} 0",
               f"writel {BASE+1:#x} 0", f"writel {BASE+4:#x} 1", f"writel {BASE+8:#x} 1",
               f"writel {BASE+0xc:#x} 1", f"writel {BASE+0x10:#x} 2",
               f"writel {BASE+0x14:#x} 0", f"writel {BASE+0x18:#x} 0",
               f"writel {BASE+0x1c:#x} 0", f"writel {BASE+0x28:#x} 0",
               f"writel {BASE+0x28:#x} 3"]
    with running(qemu, False, profile(0)) as (command, irq, reset, diagnostics):
        read, write, sample, step, _, _ = access(command)
        write(0, KEY)
        write(4, 0x100)
        write(8, KEY)
        assert sample() == 256
        for operation in invalid:
            command(operation)
            assert read(0) == read(0x10) == read(0x18) == 0
            assert read(4) == 0x100 and read(0x28) == 2 and read(0x14) == 256
        write(0, 0)  # lock status is not a key readback
        assert read(0) == 1
        for off, value in ((4, 0x200), (8, KEY), (0x10, 0xbd), (0x28, 1)):
            write(off, value)
            assert read(4) == 0x100 and read(0x10) == 0 and read(0x14) == 256
        write(0, KEY)
        write(0x10, 0xbd)
        assert sample() == 256
    lines = diagnostics[0].splitlines()
    assert lines[0] == notice(0) and len(lines) == 1 + len(invalid) + 4, diagnostics
    assert all(line.startswith("ws53-wdt: unimplemented") for line in lines[1:]), diagnostics
    return {"hardware_verified": False, "counting_requires_explicit_assumption": True,
            "assumed_low_bytes_tested": [0, 255], "clock_hz": 32000,
            "invalid_accesses_diagnosed": len(invalid), "locked_writes_diagnosed": 4,
            "invalid_low_byte_property_rejected": True,
            "pending_eoi_rejections": 2,
            "checks": ["default profile rejects counting", "count/latch/reload", "valid zero",
                       "fractional clock gating", "snapshot stall/recovery", "stop/restart",
                       "first/second expiry modes", "feed versus EOI", "mask/raw status",
                       "NMI source 1 wiring", "reset request and recovery", "32-bit maximum",
                       "invalid access preserves state", "reset retains external input levels"],
            "not_modeled": ["silicon timing", "CPU NMI delivery", "SoC reset topology/pulse",
                            "clock/reset register provisioning", "low-power/busy FSM", "exact lock coverage"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(wdt_test(parser.parse_args().qemu), indent=2))
