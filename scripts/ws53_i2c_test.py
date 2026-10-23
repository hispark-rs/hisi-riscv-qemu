#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 functional I2C profile: peers, protocol, FIFOs, clocks, IRQ and errors."""
import argparse
import json
from ws53_gpio_test import running

PROFILE = ('-global', 'ws53-i2c.assume-profile=on')
NOTICE = 'ws53-i2c: assumed profile 32MHz FIFO8 CON-addressing deferred-read-ACK disable=1us'
UNKNOWN = 'ws53-i2c: unimplemented transfer without assume-profile'
BASES = (0x52083000, 0x52084000)


class Bus:
    def __init__(self, command, index):
        self.command, self.index, self.base = command, index, BASES[index]

    def read(self, off):
        return int(self.command(f'readl {self.base+off:#x}')[0], 0)

    def write(self, off, v):
        self.command(f'writel {self.base+off:#x} {v:#x}')

    def step(self, ns=1_000_000):
        self.command(f'clock_step {ns}')

    def clock(self, on=True):
        old = int(self.command('readw 0x52000044')[0], 0)
        value = old | (0x40 << self.index) if on else old & ~(0x40 << self.index)
        self.command(f'writew 0x52000044 {value:#x}')

    def fault(self, name, value=True):
        self.command(f'set_irq_in /machine/i2c {name} {self.index} {int(value)}')

    def counter(self, name):
        return self.command.qmp('qom-get', {'path': '/machine/i2c', 'property': f'{name}{self.index}'})

    def init(self, ten=False, hold=True):
        self.clock(); self.write(4, 0); self.step(1000)
        assert self.read(0x84) == 0
        self.write(0, 0x63 | (16 if ten else 0) | (512 if hold else 0))
        self.write(0x10, 0x250 if ten else 0x50)
        self.write(0x30, 153); self.write(0x34, 159)
        self.write(0xc4, 1); self.write(4, 1)
        self.step(999); assert self.read(0x84) == 0
        self.step(1); assert self.read(0x84) == 1

    def send(self, value):
        self.write(0x1c, value); self.step()


def i2c_test(qemu):
    with running(qemu, False) as (cmd, _, _, diagnostics):
        b = Bus(cmd, 0); b.clock(); b.write(4, 1); b.step()
        assert b.read(0x84) == 0
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        for n in range(2):
            for ten in (False, True):
                reset(); b = Bus(cmd, n); b.init(ten)
                # Consecutive writes then repeated START read; no early STOP.
                for byte in (0x20, 0x12, 0x34):
                    b.send(byte)
                assert b.read(0x60) & 1 and b.counter('stops') == 0
                b.send(0x200 | 0x56)
                assert b.read(0xb8) & 0x600 == 0x600
                b.write(0xc4, 1)
                b.send(0x20)
                b.send(0x500)
                assert b.read(0x1c) == 0x12
                b.send(0x100); assert b.read(0x1c) == 0x34
                b.send(0x300); assert b.read(0x1c) == 0x56
                assert (b.counter('starts'), b.counter('restarts'), b.counter('stops')) == (2, 1, 2)
                assert (b.counter('read-acks'), b.counter('read-nacks')) == (2, 1)
                assert not b.read(0x60) & 1 and b.read(0x64) == b.read(0x68) == 0
                assert not irq.get(62+n, False)
                b.write(0xb0, 0x200); assert irq.get(62+n, False)
                b.write(0xc4, 1); assert not irq.get(62+n, False)
                # Read -> write emits last-read NACK, keeps same transaction.
                b.send(0x100); b.read(0x1c); b.send(0x400 | 0x21); b.send(0x2ab)
                assert b.counter('read-nacks') == 2
                b.send(0x21); b.send(0x700); assert b.read(0x1c) == 0xab
                # Other controller's backing peer remains independent.
                other = Bus(cmd, 1-n); other.init(); other.send(0x20); other.send(0x700)
                assert other.read(0x1c) == (0x20 ^ 0xa5)
                # Long reads cross FIFO capacity with no premature STOP/NACK.
                for k in range(100):
                    b.send(0x100 | (0x200 if k == 99 else 0))
                    expected = ((0x22+k) & 255) ^ 0xa5
                    if k == 0: expected = 0x56
                    assert b.read(0x1c) == expected
                assert b.counter('read-acks') == 101
                assert b.counter('read-nacks') == 4
                # Reset cancels queued work, IRQ and peer memory together.
                b.write(0x1c, 0x300); reset(); b.step()
                assert b.read(0x84) == b.read(0x64) == b.read(0x68) == 0
                assert not irq.get(62+n, False)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        for n in range(2):
            reset(); b = Bus(cmd, n); b.init(); b.write(0xb0, 0x4a)
            # TX capacity, overflow latched without corrupting queued commands.
            b.fault('stretch')
            for k in range(8): b.write(0x1c, k)
            assert b.read(0x64) == 8 and not b.read(0x60) & 2
            b.write(0x1c, 99); assert b.read(0xb8) & 8 and irq.get(62+n, False)
            b.fault('stretch', False); b.step(5_000_000)
            assert b.read(0x64) == 0
            b.write(0xc4, 1); assert not irq.get(62+n, False)
            b.send(0x200); b.init()
            # RX FIFO full holds ninth read until one byte is consumed.
            for k in range(8): b.send(0x100)
            assert b.read(0x68) == 8
            b.write(0x1c, 0x300); b.step()
            assert b.read(0x64) == 1 and b.read(0x68) == 8
            assert b.read(0x1c) == (8 ^ 0xa5)  # preceding writes advanced pointer to 8
            b.step(); assert b.read(0x64) == 0 and b.read(0x68) == 8
            for _ in range(8): b.read(0x1c)
            b.read(0x1c); assert b.read(0xb8) & 1
            b.init(hold=False)
            for _ in range(9): b.send(0x100)
            assert b.read(0xb8) & 2 and b.read(0x68) == 8
            # Disable clears queues but retains error until explicit clear-all.
            b.write(4, 0); b.step(1000)
            assert b.read(0x68) == 0 and b.read(0xb8) & 2
            b.write(0xc4, 1); assert not b.read(0xb8) & 0xb
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        for n in range(2):
            b = Bus(cmd, n)
            for fault, detail in (('nack-address', 1), ('nack-data', 8), ('arbitration', 4096)):
                reset(); b.init(); b.write(0xb0, 64); b.fault(fault); b.send(0x200)
                assert b.read(0x7c) == detail and b.read(0xb4) == 64 and irq.get(62+n, False)
                assert b.read(0x64) == 0 and not b.read(0x60) & 1
                b.fault(fault, False); b.write(4, 0); b.step(1000)
                assert b.read(0x7c) == detail
                b.write(0xc4, 1); assert b.read(0x7c) == 0 and not irq.get(62+n, False)
                b.write(4, 1); b.step(1000); b.send(0x200)
                assert not b.read(0xb8) & 64 and b.read(0xb8) & 0x200
            reset(); b.init(True); b.fault('nack-address'); b.send(0x300)
            assert b.read(0x7c) == 2; b.fault('nack-address', False)
            # Preserve remaining command time over gate-off, no fresh START.
            reset(); b.init(); b.write(0x1c, 0x300); b.step(10_000)
            b.clock(False); b.step(10_000_000)
            assert b.read(0x68) == 0
            b.clock(); b.step(170_562); assert b.read(0x68) == 0
            b.step(1); assert b.read(0x68) == 1
            assert b.counter('starts') == b.counter('stops') == 1
            # Gate loss also stalls disable synchronization; keep enabled until clock returns.
            b.fault('clock', False); b.write(4, 0); b.step()
            assert b.read(0x84) == 1
            b.fault('clock'); b.step(999); assert b.read(0x84) == 1
            b.step(1); assert b.read(0x84) == 0
            # Stretched bus can be shut down, and a later enable can transfer again.
            b.init(); b.fault('stretch'); b.write(0x1c, 0x300); b.step()
            assert b.read(0x68) == 0 and b.read(0x64) == 1
            b.write(4, 0); b.step(1000); assert b.read(0x64) == b.read(0x84) == 0
            b.fault('stretch', False); b.init(); b.send(0x300); assert b.read(0x68) == 1
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        for n in range(2):
            b = Bus(cmd, n)
            # Both other source speed profiles must use their own count slots.
            for mode, off, hi, lo, elapsed in ((2, 0x3c, 32, 40, 45563), (3, 0x44, 9, 23, 23063)):
                reset(); b.init(); b.write(4, 0); b.step(1000)
                b.write(0, 0x261 | (mode << 1)); b.write(off, hi); b.write(off+4, lo)
                b.write(4, 1); b.step(1000); b.write(0x1c, 0x300)
                b.step(elapsed-1); assert b.read(0x68) == 0
                b.step(1); assert b.read(0x68) == 1
            # A small address in ten-bit mode is not silently treated as 7-bit.
            reset(); b.init(True); b.write(4, 0); b.step(1000)
            b.write(0x10, 0x50); b.write(4, 1); b.step(1000); b.send(0x300)
            assert b.read(0x7c) == 2
            # Explicit user ABORT flushes pending commands, preserving source.
            b.init(); b.fault('stretch'); b.write(0x1c, 0x300)
            b.write(4, 3); b.step(1000)
            assert b.read(0x78) == 1 and b.read(0x74) == 1
            assert b.read(0x64) == b.read(0x68) == 0
            b.fault('stretch', False); b.write(0xc4, 1)
            b.send(0x300); assert b.read(0x68) == 1
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    invalid_count = 0
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        for n in range(2):
            reset(); b = Bus(cmd, n); b.init()
            # Bad widths/alignment, read-only writes, unimplemented features and
            # disputed selective clear must report, not silently return success.
            for text in (
                f'readb {b.base:#x}', f'readw {b.base:#x}', f'readl {b.base+1:#x}',
                f'readq {b.base:#x}', f'writeb {b.base:#x} 1',
                f'writel {b.base+0x60:#x} 1', f'writel {b.base+0x84:#x} 1',
                f'readl {b.base+0xc4:#x}', f'writel {b.base+0xc0:#x} 0xffffffff',
                f'writel {b.base+0xc4:#x} 0', f'writel {b.base+0xa0:#x} 1',
                f'writel {b.base+0xb0:#x} 0x8000', f'writel {b.base+0x50:#x} 8',
                f'writel {b.base+0x10:#x} 0x51', f'writel {b.base+0x14:#x} 1',
                'readl 0x52000044', 'writel 0x52000044 0', 'writew 0x52000044 0',
            ):
                cmd(text); invalid_count += 1
            assert b.read(0x10) == 0x50 and b.read(0xa0) == 0 and b.read(0x50) == 0
            b.send(0x300); assert b.read(0x1c) == 0xa5
            b.write(4, 0); b.step(1000)
            b.write(0x30, 0); b.write(4, 1); invalid_count += 1
            b.step(); assert b.read(0x84) == 0  # invalid timing cannot look enabled
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == invalid_count+1, diagnostics
    assert all('unimplemented' in line for line in lines[1:]), diagnostics
    return {'instances': 2, 'address_modes': [7, 10], 'fifo_depth': 8,
            'peer': 'volatile 256-byte register device', 'irq': [62, 63],
            'invalid_mmio_diagnostics': invalid_count,
            'silicon_verified': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    print(json.dumps(i2c_test(p.parse_args().qemu), indent=2))
