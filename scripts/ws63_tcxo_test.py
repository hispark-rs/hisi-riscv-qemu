#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS63/BS21 TCXO control, latched words and virtual-time regression; no hardware."""
import argparse
import json
from ws53_gpio_test import running


def check(qemu):
    results = []
    for machine, base, hz, width in (("ws63", 0x440004c0, 24000000, 32),
                                    ("bs21", 0x57000200, 32000000, 16)):
        with running(qemu, machine=machine) as (cmd, _, _, diagnostics):
            def read(off=0):
                return int(cmd(f"readl {base + off:#x}")[0], 0)
            def write(value):
                cmd(f"writel {base:#x} {value:#x}")
            def words():
                return sum(read(4 + i * 4) << (i * width) for i in range(64 // width))
            def sample():
                write(5)
                assert read() == 0x14
                return words()
            # Direct-kernel boot inherits a running counter, not a silicon POR claim.
            assert read() & 4
            write(6)  # clear while enabled
            assert read() == 4
            a = sample()
            cmd("clock_step 1000000")
            assert words() == a, "unrefreshed words must be one stable snapshot"
            assert sample() - a == hz // 1000
            write(0)  # disable and freeze
            assert read() == 0x10
            cmd("clock_step 1000000")
            write(1)  # VALID cannot be fabricated while disabled
            assert read() == 0
            frozen = sample()
            assert frozen - a == hz // 1000
            cmd("clock_step 180000000000")
            assert sample() - frozen == hz * 180, "full 64-bit word assembly across low wrap"
            write(6)
            assert sample() == 0
        assert not any(line for line in diagnostics if line.strip()), diagnostics
        results.append(dict(machine=machine, passed=True, frequency=hz))
    return dict(cases=results, hardware_accessed=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qemu", required=True)
    print(json.dumps(check(p.parse_args().qemu), indent=2))
