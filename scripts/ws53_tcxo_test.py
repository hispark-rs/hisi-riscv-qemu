#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 TCXO raw ticks, snapshot/clock faults and command-readback alternatives."""
import argparse
import json
import subprocess
from ws53_gpio_test import running

BASE = 0x57000200
MASK = (1 << 64) - 1


def options(initial=0, hz=32000000, sticky=False):
    return ("-global", f"ws53-tcxo.initial-count={initial}",
            "-global", f"ws53-tcxo.clock-frequency={hz}",
            "-global", f"ws53-tcxo.sticky-commands={'on' if sticky else 'off'}")


def access(command):
    def read(off):
        return int(command(f"readl {BASE+off:#x}")[0], 0)
    def write(value):
        command(f"writel {BASE:#x} {value:#x}")
    def words():
        result = 0
        for index in (3, 2, 1, 0):
            word = read(4 + index * 4)
            assert 0 <= word <= 0xffff
            result = result << 16 | word
        return result
    def sample():
        write(5)
        assert read(0) & 0x10
        return words()
    def step(ns):
        command(f"clock_step {ns}")
    def input_(name, high):
        command(f"set_irq_in /machine/tcxo {name} 0 {int(high)}")
    return read, write, words, sample, step, input_


def tcxo_test(qemu):
    failed = subprocess.run([qemu, "-M", "ws53", "-accel", "qtest", "-display", "none",
                             "-nodefaults", *options(hz=0)], capture_output=True, text=True, timeout=5)
    assert failed.returncode != 0 and "clock-frequency must be nonzero" in failed.stderr
    for sticky in (False, True):
        initial = 0x123456789abcdef0
        with running(qemu, False, options(initial=initial, sticky=sticky)) as (command, _, reset, diagnostics):
            read, write, words, sample, step, input_ = access(command)
            assert read(0) == 0
            write(0x10)  # read-only VALID cannot be asserted by a write
            assert read(0) == 0
            step(1_000_000)
            write(4)
            assert sample() == initial  # enable must not clear the inherited epoch
            step(1000)
            assert words() == initial  # reads are latched even across many wraps of low bits
            assert sample() == initial + 32
            write(6)
            assert read(0) == (6 if sticky else 4)
            assert sample() == 0
            step(125)
            assert sample() == 4  # a refresh must not replay the prior CLEAR command
            step(31)
            input_("clock", False)
            write(5)
            assert not read(0) & 0x10
            step(10_000_000_000)
            input_("clock", True)
            assert words() == 4 and read(0) & 0x10
            step(1)
            assert sample() == 5  # retained sub-cycle phase (31ns + 1ns)
            input_("stall-snapshot", True)
            write(5)
            assert not read(0) & 0x10
            step(1000)
            input_("stall-snapshot", False)
            assert read(0) & 0x10 and words() == 37
            write(0)
            step(1000)
            write(4)
            assert sample() == 37  # disabled counter does not advance
            input_("clock", False)
            input_("stall-snapshot", True)
            reset()
            assert read(0) == 0
            write(5)
            input_("clock", True)
            assert not read(0) & 0x10
            input_("stall-snapshot", False)
            assert words() == initial
        assert diagnostics == [""], diagnostics

    for initial, hz, ns in ((0xffff, 32000000, 125), (0xffffffff, 32000000, 125),
                            (0xffffffffffff, 32000000, 125), (MASK - 1, 32000000, 125),
                            (MASK - 10, 0xffffffff, 10_000_000_001), (0, 1, 999_999_999)):
        with running(qemu, False, options(initial, hz)) as (command, _, _, diagnostics):
            _, write, _, sample, step, _ = access(command)
            write(4)
            assert sample() == initial
            step(ns)
            assert sample() == (initial + ns * hz // 1_000_000_000) & MASK
        assert diagnostics == [""], diagnostics

    invalid = [f"readb {BASE:#x}", f"readw {BASE:#x}", f"readq {BASE:#x}",
               f"readl {BASE+1:#x}", f"writeb {BASE:#x} 0", f"writew {BASE:#x} 0",
               f"writeq {BASE:#x} 0", f"writel {BASE+1:#x} 0",
               f"writel {BASE:#x} 8", f"writel {BASE:#x} 7",
               *[f"writel {BASE+off:#x} 0" for off in (4, 8, 12, 16)]]
    with running(qemu, False, options(0x123456789abcdef0)) as (command, _, _, diagnostics):
        read, write, words, sample, _, input_ = access(command)
        write(4)
        expected = sample()
        for operation in invalid:
            command(operation)
            assert read(0) == 0x14 and words() == expected, operation
        input_("stall-snapshot", True)
        write(5)
        # Invalid data reads cannot silently look like an accepted zero sample.
        for off in (4, 8, 12, 16):
            read(off)
            assert not read(0) & 0x10
    lines = diagnostics[0].splitlines()
    assert len(lines) == len(invalid) + 4, diagnostics
    assert all(line.startswith("ws53-tcxo: unimplemented") for line in lines), diagnostics
    return {"access_bits": 32, "data_bits_per_slot": 16, "data_slots": 4,
            "reference_clock_hz": 32000000, "command_readback_profiles": ["pulse", "sticky"],
            "wrap_and_rate_cases": 6, "invalid_accesses_diagnosed": len(invalid) + 4,
            "checks": ["enable retains epoch", "latched not live words", "explicit clear",
                       "valid zero", "fractional gate pause", "snapshot stall/recovery",
                       "read-only valid bit", "16/32/48/64-bit wrap", "full rate arithmetic",
                       "reset retains external faults", "invalid access preserves snapshot"],
            "hardware_verified": False, "control_core_window_modeled": False,
            "sleep_compensation_modeled": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    print(json.dumps(tcxo_test(parser.parse_args().qemu), indent=2))
