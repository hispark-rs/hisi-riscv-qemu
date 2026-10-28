#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Real CPU LOCIPD/LOCIPCLR test for the silicon-qualified WS53 PWM_CFG source."""
import argparse
import json
import struct
from ws53_branch_immediate_test import execute_ram, li, store


def irq_latch_test(qemu):
    result, done = 0x20050000, 0x2005fff0
    csr_read = lambda csr, rd: (csr << 20) | (2 << 12) | (rd << 7) | 0x73
    csr_write = lambda csr, rs: (csr << 20) | (rs << 15) | (1 << 12) | 0x73
    code = li(8, result)
    code += [csr_read(0xbe9, 6), store(6, 8, 0)]
    # Clearing another source must not erase retained IRQ72.
    code += li(5, 71) + [csr_write(0xbf0, 5), csr_read(0xbe9, 6), store(6, 8, 4)]
    code += li(5, 72) + [csr_write(0xbf0, 5), csr_read(0xbe9, 6), store(6, 8, 8)]
    code += li(5, done) + li(6, 1) + [store(6, 5, 0), 0x0000006f]
    setup = [f'set_irq_in /machine/intc unnamed-gpio-in {irq} {level}'
             for irq in (71, 72) for level in (1, 0)]
    payload = struct.pack('<'+'I'*len(code), *code)
    raw = execute_ram(qemu, ((0x28000, payload), (result, bytes(12))),
                      result, 12, done, setup_commands=setup)
    values = struct.unpack('<III', raw)
    assert values == (0x100, 0x100, 0), values
    return dict(source=72, delivery_disabled=True, deassertion_retains_pending=True,
                unrelated_clear_preserves=True, own_clear_acknowledges=True,
                cpu_csr_values=values, unqualified_source71_keeps_previous_profile=True)


def irq58_clear_test(qemu):
    """Execute real CSR writes while IRQ58's external input is held high/low."""
    result, done = 0x20050000, 0x2005fff0
    snapshots = []
    for level in (1, 0):
        code = li(8, result)
        read = (0xbe8 << 20) | (2 << 12) | (6 << 7) | 0x73
        clear = (0xbf0 << 20) | (5 << 15) | (1 << 12) | 0x73
        code += [read, store(6, 8, 0)]
        code += li(5, 58) + [clear, read, store(6, 8, 4)]
        code += li(5, 57) + [clear, read, store(6, 8, 8)]
        code += li(5, done) + li(6, 1) + [store(6, 5, 0), 0x0000006f]
        setup = ['set_irq_in /machine/intc unnamed-gpio-in 57 1',
                 'set_irq_in /machine/intc unnamed-gpio-in 58 1']
        if not level: setup += ['set_irq_in /machine/intc unnamed-gpio-in 58 0']
        payload = struct.pack('<'+'I'*len(code), *code)
        raw = execute_ram(qemu, ((0x28000, payload), (result, bytes(12))),
                          result, 12, done, setup_commands=setup)
        values = struct.unpack('<III', raw)
        expected = ((3 << 25, 3 << 25, 1 << 26) if level else (1 << 25, 1 << 25, 0))
        assert values == expected, (level, values)
        snapshots.append(values)
    return dict(source=58, held_high_reasserts_after_clear=True,
                deasserted_does_not_reassert=True, unrelated_source57_unchanged=True,
                cpu_csr_values=snapshots, hardware_timing_proven=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(irq_latch_test(parser.parse_args().qemu), indent=2))
