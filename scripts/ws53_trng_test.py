#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 deterministic TRNG test profile: FIFO, timing, faults; no entropy claim."""
import argparse
import json
import subprocess
from ws53_gpio_test import running

BASE = 0x52024000
RING = 0x52001600
PROFILE = ('-global', 'ws53-trng.assume-profile=on')
NOTICE = 'ws53-trng: assumed FIFO8 pair-ready startup200us pair10us deterministic-test-source; NOT entropy'


def sequence(seed, count):
    words = []
    for _ in range(count):
        seed ^= (seed << 13) & 0xffffffff
        seed ^= seed >> 17
        seed ^= (seed << 5) & 0xffffffff
        words.append(seed)
    return words


class Trng:
    def __init__(self, cmd): self.cmd = cmd
    def status(self): return int(self.cmd(f'readl {BASE+0x104:#x}')[0], 0)
    def word(self): return int(self.cmd(f'readl {BASE+0x100:#x}')[0], 0)
    def ring(self, value): self.cmd(f'writew {RING:#x} {value:#x}')
    def ring_value(self): return int(self.cmd(f'readw {RING:#x}')[0], 0)
    def step(self, ns): self.cmd(f'clock_step {ns}')
    def fault(self, index, value=True): self.cmd(f'set_irq_in /machine/trng fault {index} {int(value)}')
    def clock(self, value): self.cmd(f'set_irq_in /machine/trng clock 0 {int(value)}')
    def count(self, name): return self.cmd.qmp('qom-get', {'path': '/machine/trng', 'property': name})


def trng_test(qemu):
    invalid = subprocess.run([qemu, '-M', 'ws53', '-accel', 'qtest', '-display', 'none', '-nodefaults',
                              '-global', 'ws53-trng.test-seed=0'], capture_output=True, text=True, timeout=5)
    assert invalid.returncode != 0 and 'test seed must be nonzero' in invalid.stderr
    with running(qemu, False) as (cmd, _, _, diagnostics):
        t = Trng(cmd); t.ring(0xa5a5); t.step(1000000)
        assert t.ring_value() == 0xa5a5 and t.status() == t.count('generated') == 0
        assert t.word() == 0
    assert diagnostics[0].splitlines() == [
        'ws53-trng: unimplemented generation without assume-profile',
        'ws53-trng: unimplemented/unavailable read +0x100 size=4'], diagnostics

    # Explicit seeds are test inputs, never platform randomness.
    seeds = (1, 0x12345678, 0x80000000, 0xffffffff)
    for seed in seeds:
        with running(qemu, False, PROFILE + ('-global', f'ws53-trng.test-seed={seed}')) as (cmd, _, reset, diagnostics):
            t = Trng(cmd); expected = sequence(seed, 128)
            assert t.ring_value() == t.status() == t.count('fifo-count') == 0
            t.ring(0xa5a5); t.step(199999); assert t.status() == 0
            t.step(1)
            for n in range(64):
                assert t.status() == t.status() == 3
                assert t.count('consumed') == 2*n
                assert t.word() == expected[2*n]
                assert t.status() == 0 and t.count('fifo-count') == 1
                assert t.word() == expected[2*n+1]
                assert t.status() == 0
                if n != 63:
                    t.step(9999); assert t.status() == 0
                    t.step(1)
            assert t.count('generated') == t.count('consumed') == 128
            reset(); assert t.ring_value() == t.count('generated') == 0
            t.ring(1); t.step(200000)
            assert [t.word(), t.word()] == expected[:2]
        assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        t = Trng(cmd); expected = sequence(1, 16)
        t.ring(0xa5a5); t.step(230000)
        assert t.count('fifo-count') == t.count('generated') == 8
        t.step(1000000); assert t.count('generated') == 8
        assert t.word() == expected[0]; t.step(10000)
        assert t.count('fifo-count') == 7 and t.count('generated') == 8
        assert t.word() == expected[1]; t.step(10000)
        assert t.count('fifo-count') == 8 and t.count('generated') == 10
        assert [t.word() for _ in range(8)] == expected[2:10]
        # Same-value ring writes do not restart a running stream or clear FIFO.
        t.step(10000); t.ring(0x55a5)
        assert t.ring_value() == 0x55a5 and t.count('fifo-count') == 2
        t.ring(0x55a4)
        assert t.status() == t.count('fifo-count') == 0 and t.count('discarded') == 2
        t.step(1000000); assert t.count('generated') == 12
        t.ring(0x55a5); t.step(199999); assert t.status() == 0
        t.step(1); assert [t.word(), t.word()] == expected[12:14]
        reset(); assert t.count('discarded') == t.count('consumed') == 0
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        t = Trng(cmd); t.ring(1); t.step(100000); t.clock(False); t.step(1000000)
        assert t.count('generated') == t.status() == 0
        t.clock(True); t.step(99999); assert t.status() == 0
        t.step(1); assert t.status() == 3
        t.clock(False); assert t.status() == 0 and t.count('fifo-count') == 2
        t.clock(True); assert [t.word(), t.word()] == sequence(1, 2)
        t.step(3000); t.fault(0); t.step(1000000)
        assert t.count('generated') == 2
        t.fault(0, False); t.step(6999); assert t.status() == 0
        t.step(1); assert t.status() == 3
        # Source stall does not remove already-produced data.
        t.fault(0); assert t.status() == 3
        t.fault(1); assert t.status() == 2
        t.fault(2); assert t.status() == 0
        t.fault(1, False); assert t.status() == 1
        t.fault(2, False); t.fault(6)
        assert [t.status() for _ in range(4)] == [3, 0, 3, 0]
        assert t.count('consumed') == 2 and t.count('fifo-count') == 2
        t.fault(6, False); assert [t.word(), t.word()] == sequence(1, 4)[2:]
        # Reset cancels a paused timer and stream, while external fault persists.
        reset(); t.ring(1); t.step(1000000); assert t.status() == t.count('generated') == 0
        t.fault(0, False); t.step(199999); assert t.status() == 0
        t.step(1); assert [t.word(), t.word()] == sequence(1, 2)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    for fault in (3, 4, 5):
        with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
            t = Trng(cmd); t.fault(fault); t.ring(1); t.step(200000)
            expected = 0 if fault == 3 else 0xffffffff if fault == 4 else sequence(1, 2)[0]
            assert [t.word(), t.word()] == [expected, expected]
            t.fault(fault, False); t.step(10000)
            assert [t.word(), t.word()] == sequence(1, 4)[2:]
        assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    invalid_accesses = 0
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        t = Trng(cmd); t.ring(1); t.step(200000)
        commands = [f'readb {BASE+0x100:#x}', f'readw {BASE+0x100:#x}', f'readq {BASE+0x100:#x}',
                    f'readl {BASE+0x101:#x}', f'readl {BASE+0x108:#x}',
                    f'writel {BASE+0x100:#x} 1', f'writel {BASE+0x104:#x} 3',
                    f'readl {BASE+0x174:#x}', f'writel {BASE+0x174:#x} 1', f'writel {BASE+0x178:#x} 27',
                    f'readb {RING:#x}', f'readl {RING:#x}', f'readq {RING:#x}', f'readw {RING+1:#x}',
                    f'writeb {RING:#x} 0', f'writel {RING:#x} 0', f'writeq {RING:#x} 0', f'writew {RING+2:#x} 0']
        for command in commands:
            cmd(command); invalid_accesses += 1
            assert t.count('fifo-count') == 2 and t.count('consumed') == 0 and t.ring_value() == 1
        assert [t.word(), t.word()] == sequence(1, 2)
        assert t.word() == 0; invalid_accesses += 1  # underflow is diagnosed
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == invalid_accesses+1, diagnostics
    assert all('ws53-trng: unimplemented' in line for line in lines[1:]), diagnostics
    return {'seeded_words_checked': len(seeds)*128, 'seed_reset_replay': True,
            'fifo_backpressure_and_partial_pair': True, 'ring_disable_flush_and_reenable': True,
            'clock_and_source_stall_resume': True, 'ready_flags_race_and_bad_pairs': True,
            'invalid_access_diagnostics': invalid_accesses, 'hardware_verified': False,
            'entropy_quality_claimed': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(trng_test(parser.parse_args().qemu), indent=2))
