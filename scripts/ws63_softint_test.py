#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS63 SYS_CTL1 software sources; register/IRQ model only, no hardware."""
import argparse
import json
from ws53_gpio_test import running


def test(qemu):
    with running(qemu, False, [], machine='ws63') as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        def read(off): return int(cmd(f'readl {0x44000150+off:#x}')[0], 0)
        def write(off, val): cmd(f'writel {0x44000150+off:#x} {val:#x}')
        def levels():
            read(12)
            return [irq.get(36+i, False) for i in range(4)]
        assert read(0) == read(12) == 0
        write(4, 1)
        assert read(12) == 1 and levels() == [False]*4
        write(0, 15)
        assert levels() == [True, False, False, False]
        for i in range(4):
            write(4, 1 << i)
            assert read(4) == 0 and read(12) == (1 << (i+1))-1
        assert levels() == [True]*4
        write(8, 2)
        assert read(8) == 0 and read(12) == 13
        assert levels() == [True, False, True, True]
        write(0, 0)
        assert read(12) == 13 and levels() == [False]*4
        write(4, 0); write(8, 0)
        write(0, 0xffffffff)
        assert read(0) == 15 and levels() == [True, False, True, True]
        write(12, 0)  # RO status must not become an implicit clear command.
        assert read(12) == 13
        write(8, 0xffffffff)
        assert read(12) == 0 and levels() == [False]*4
        # The small overlay must leave the existing TCXO counter window intact.
        control = int(cmd('readl 0x440004c0')[0], 0)
        assert control & 4
        write(4, 15)
        reset()
        assert read(0) == read(12) == 0 and levels() == [False]*4
    assert diagnostics == ['ws63-softint: write to read-only status\n'], diagnostics
    return dict(passed=True, sources=4, irqs=list(range(36,40)), hardware_accessed=False)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(test(parser.parse_args().qemu), indent=2))
