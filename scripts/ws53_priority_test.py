#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Execute SDK LOCIPRI indexing and mixed UART39/MDMA58 delivery on TCG."""
import argparse
import json
import struct
from ws53_branch_immediate_test import execute_ram, li, store, branch


def csr(number, source, funct=1, dest=0):
    return number << 20 | source << 15 | funct << 12 | dest << 7 | 0x73


def run(qemu):
    results = []
    base, vector, output, done = 0x28000, 0x29000, 0x20050000, 0x2005fff0
    for first, second in [(58, 39), (39, 58)]:
        # x8 is the ISR log pointer, x9 its count. The handler owns x10..x12.
        code = li(8, output) + li(9, 0) + li(5, vector) + [csr(0x305, 5)]
        # CSR reset readback must match the CPU's arbitration state.
        code += [csr(0xbc1, 0, 2, 6), store(6, 8, 16)]
        for reg in range(16):
            code += [csr(0xbc0 + reg, 0)]
        for irq, priority in [(first, 3), (second, 1)]:
            order = irq - 26  # SDK interrupt.c / vectors.h, not absolute IRQ.
            code += li(5, priority << ((order % 8) * 4)) + [csr(0xbc0 + order // 8, 5)]
        code += li(5, (1 << (39 - 32)) | (1 << (58 - 32))) + [csr(0xbe0, 5)]
        code += li(5, 1) + [csr(0xbfe, 5), csr(0x300, 8, 6)]
        # Threshold1 admits priority3 only. A wrong index never reaches count1.
        code += li(7, 1)
        code += [branch(9, 7, 1, 0)]
        code += [csr(0xbfe, 0)] + li(7, 2)
        code += [branch(9, 7, 1, 0)]
        code += [csr(0x300, 8, 7)] + li(5, done) + li(6, 1) + [store(6, 5, 0), 0x6f]
        handler = [csr(0x342, 0, 2, 10), store(10, 8, 0),
                   0x00440413, 0x00148493,
                   0x01f57593, 0x00100613, 0x00b61633, csr(0xbe0, 12, 3),
                   csr(0xbf0, 10), 0x30200073]
        packed = lambda words: struct.pack('<' + 'I' * len(words), *words)
        raw = execute_ram(qemu, [(base, packed(code)), (vector, packed(handler)),
                                (output, bytes(32))], output, 32, done,
                          [f'set_irq_in /machine/intc unnamed-gpio-in {irq} 1' for irq in (39, 58)])
        values = struct.unpack('<8I', raw)
        assert values[:2] == (0x80000000 | first, 0x80000000 | second), values
        assert values[4] == 0x11111111, values
        results.append({'order': [first, second], 'threshold': [1, 0], 'reset_priority': hex(values[4])})
    return {'cases': results, 'tcg_execution': True, 'hardware_accessed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(run(parser.parse_args().qemu), indent=2))
