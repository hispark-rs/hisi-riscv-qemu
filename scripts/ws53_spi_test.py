#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 SPI0/QSPI1: distinct maps, virtual words, FIFO, IRQ, gates and failure recovery."""
import argparse
import json
from ws53_gpio_test import running
from ws53_i2c_test import Bus as I2cBus, NOTICE as I2C_NOTICE

PROFILE = ('-global', 'ws53-spi.assume-profile=on')
NOTICE = 'ws53-spi: assumed profile 32MHz FIFO8 XOR-peer enable-crossing=1us'
UNKNOWN = 'ws53-spi: unimplemented transfer without assume-profile'
# Independent register offsets from audited source/PAC, not imported from model.
MAPS = (
    dict(enable=0, control=4, fifo=0x2c, status=0xe4, mask=0xc4, raw=0xbc,
         masked=0xc0, select=0xc8, tx_level=0xd0, rx_level=0xdc,
         tx_threshold=0xcc, rx_threshold=0xd8, clear_tx=0xd4, clear_rx=0xf4,
         clear_under=0xe0, clear_multi=0xe8, clear=0xf8, dma=0x18,
         enhanced=0xc, sample=0x24, version=0xec),
    dict(enable=8, control=0, fifo=0x60, status=0x28, mask=0x2c, raw=0x34,
         masked=0x30, select=0x10, tx_level=0x20, rx_level=0x24,
         tx_threshold=0x18, rx_threshold=0x1c, clear_tx=0x38, clear_rx=0x3c,
         clear_under=0x40, clear_multi=0x44, clear=0x48, dma=0x4c,
         enhanced=0xf4, sample=0xf0, version=0x5c),
)
GATES = (((0x52000044, 0x800), (0x5200055c, 1)),
         ((0x52000548, 0x80), (0x52000574, 1), (0x52001240, 0x2a)))


class Bus:
    def __init__(self, command, index):
        self.cmd, self.index = command, index
        self.base = (0x52087000, 0x5208d000)[index]
        self.map = MAPS[index]
        self.irq = (65, 43)[index]

    def read(self, reg):
        off = self.map[reg] if isinstance(reg, str) else reg
        return int(self.cmd(f'readl {self.base+off:#x}')[0], 0)

    def write(self, reg, value):
        off = self.map[reg] if isinstance(reg, str) else reg
        self.cmd(f'writel {self.base+off:#x} {value:#x}')

    def step(self, ns=1000000):
        self.cmd(f'clock_step {ns}')

    def gate(self, address, mask, on=True):
        old = int(self.cmd(f'readw {address:#x}')[0], 0)
        value = old | mask if on else old & ~mask
        self.cmd(f'writew {address:#x} {value:#x}')

    def fault(self, name, value=True):
        self.cmd(f'set_irq_in /machine/spi {name} {self.index} {int(value)}')

    def bits(self, canonical):
        if self.index:
            return canonical
        return sum(out for flag, out in ((1, 128), (2, 256), (4, 4), (8, 2), (16, 1), (32, 32768))
                   if canonical & flag)

    def frames(self):
        return self.cmd.qmp('qom-get', {'path': '/machine/spi', 'property': f'frames{self.index}'})

    def init(self, width=8, mode=0, enable=True):
        for addr, mask in GATES[self.index]:
            self.gate(addr, mask)
        if self.index == 0:
            self.cmd('writew 0x52000950 1')
        self.write('enable', 0); self.step(1000)
        control = ((width-1) << (16 if self.index else 13)) | (mode << (6 if self.index else 3))
        self.write('control', control); self.write(0x14, 4)
        self.write('mask', 0); self.read('clear'); self.write('select', 1)
        if enable:
            self.write('enable', 1); self.step(1000)


def spi_test(qemu):
    with running(qemu, False) as (cmd, _, _, diagnostics):
        b = Bus(cmd, 0); b.init(enable=False); b.write('enable', 1); b.step()
        assert b.read('enable') == 0 and b.frames() == 0
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        for n in range(2):
            for width in (8, 32):
                for mode in range(4):
                    reset(); b = Bus(cmd, n); b.init(width, mode)
                    assert b.read('control') == ((width-1) << (16 if n else 13)) | (mode << (6 if n else 3))
                    b.write('mask', b.bits(16))
                    for k in range(100):
                        word = (0x12345678 + k) & ((1 << width)-1)
                        # All 36 addresses alias one queue; they are not FIFO capacity.
                        b.write(b.map['fifo'] + (k % 36)*4, word)
                        assert b.read('tx_level') == 1 and b.read('rx_level') == 0
                        b.step(width*125-1); assert b.read('rx_level') == 0
                        b.step(1); assert b.read('tx_level') == 0 and b.read('rx_level') == 1
                        assert irq.get(b.irq, False) and b.read('masked') == b.bits(16)
                        assert b.read(b.map['fifo'] + ((35-k) % 36)*4) == ((word ^ 0xa5a5a5a5) & ((1 << width)-1))
                        assert not irq.get(b.irq, False)
                    assert b.frames() == 100
                    b.write('enable', 0); b.step(1000)
                    assert not b.read('raw') & b.bits(1)
            # FIFO overflow and read-clear (writes have no effect).
            reset(); b = Bus(cmd, n); b.init(); b.fault('stretch')
            b.write('mask', b.bits(2))
            for word in range(9): b.write('fifo', word)
            assert b.read('tx_level') == 8 and b.read('raw') & b.bits(2)
            assert irq.get(b.irq, False)
            b.write('clear_tx', 1); assert b.read('raw') & b.bits(2)
            assert b.read('clear_tx') == 1 and not irq.get(b.irq, False)
            assert b.read('clear_tx') == 0
            b.fault('stretch', False); b.step(8000)
            assert b.read('rx_level') == 8 and b.read('status') & (16 if n else 32)
            b.write('fifo', 9); b.step(1000)
            assert b.read('rx_level') == 8 and b.read('raw') & b.bits(8)
            assert b.read('clear_rx') == 1
            assert [b.read('fifo') for _ in range(8)] == [k ^ 0xa5 for k in range(8)]
            b.write('mask', b.bits(4)); assert b.read('fifo') == 0
            assert irq.get(b.irq, False) and b.read('clear_under') == 1
            assert not irq.get(b.irq, False)
            # Threshold status is a level: clearing errors must not clear it.
            b.write('mask', b.bits(1)); assert irq.get(b.irq, False)
            b.read('clear'); assert irq.get(b.irq, False)
            b.write('mask', 0)
            # Collision is a fault, not synthetic successful data.
            b.fault('collision'); b.write('fifo', 99); b.step(1000)
            assert b.read('rx_level') == 0 and b.read('raw') & b.bits(32)
            b.write('mask', b.bits(32)); assert irq.get(b.irq, False)
            assert b.read('clear_multi') == 1 and not irq.get(b.irq, False)
            b.fault('collision', False)
            # Every required gate pauses the exact remaining frame duration.
            for address, mask in GATES[n]:
                b.write('fifo', 0x12); b.step(400); b.gate(address, mask, False)
                b.step(); assert b.read('rx_level') == 0 and b.read('tx_level') == 1
                b.gate(address, mask); b.step(599); assert b.read('rx_level') == 0
                b.step(1); assert b.read('fifo') == 0xb7
            # Stopped source also prevents disable completion; control readback
            # alone is not proof of idle. Resume allows bounded HAL recovery.
            b.write('fifo', 3); b.fault('clock', False); b.write('enable', 0); b.step()
            assert b.read('enable') == 0 and b.read('tx_level') == 1
            assert b.read('status') & (1 if n else 0x8000)
            b.fault('clock'); b.step(1000)
            assert b.read('tx_level') == b.read('rx_level') == 0
            assert not b.read('status') & (1 if n else 0x8000)
            b.init(); b.fault('disable-stall'); b.write('enable', 0); b.step()
            assert b.read('status') & (1 if n else 0x8000)
            b.fault('disable-stall', False); b.step(1000)
            assert not b.read('status') & (1 if n else 0x8000)
            # Reset cancels timer, queues and errors; external fault survives.
            b.init(); b.write('fifo', 4); b.fault('clock', False); reset(); b.step()
            assert b.frames() == b.read('tx_level') == b.read('rx_level') == b.read('raw') == 0
            b.init(); b.write('fifo', 5); b.step(); assert b.frames() == 0
            b.fault('clock'); b.step(); assert b.read('fifo') == 0xa0
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    # One shared clock register must preserve all three consumers. SPI gate
    # changes during an I2C transfer cannot reset/stall I2C or overlap its MMIO.
    with running(qemu, False, (*PROFILE, '-global', 'ws53-i2c.assume-profile=on')) as (cmd, _, _, diagnostics):
        i = I2cBus(cmd, 0); i.init(); s = Bus(cmd, 0); s.init()
        i.write(0x1c, 0x300); s.write('fifo', 0x42)
        s.gate(0x52000044, 0x800, False); s.step()
        assert i.read(0x1c) == 0xa5 and s.read('tx_level') == 1
        s.gate(0x52000044, 0x800); s.step(); assert s.read('fifo') == 0xe7
        assert int(cmd('readw 0x52000044')[0], 0) == 0x851
    assert set(diagnostics[0].splitlines()) == {NOTICE, I2C_NOTICE}, diagnostics

    invalid = 0
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        for n in range(2):
            reset(); b = Bus(cmd, n); b.init(enable=False)
            commands = [f'readb {b.base:#x}', f'readw {b.base:#x}', f'readl {b.base+1:#x}',
                        f'readq {b.base:#x}', f'writeb {b.base:#x} 1',
                        f'readl {b.base+b.map["version"]:#x}',
                        f'writel {b.base+b.map["status"]:#x} 1',
                        f'writel {b.base+b.map["dma"]:#x} 1',
                        f'writel {b.base+b.map["enhanced"]:#x} 1',
                        f'writel {b.base+b.map["control"]:#x} 0',
                        f'writel {b.base+b.map["select"]:#x} 2',
                        f'writel {b.base+b.map["sample"]:#x} 256',
                        f'writel {b.base+b.map["mask"]:#x} 0xffffffff',
                        f'writel {b.base+b.map["rx_threshold"]:#x} 8',
                        f'writel {b.base+0x14:#x} 3',
                        'writel 0x5200055c 1', 'writew 0x52000574 2',
                        'writew 0x52001240 0x200', 'writew 0x52000044 0']
            for command in commands: cmd(command); invalid += 1
            b.write('enable', 1); b.step(1000)
            b.write('control', b.read('control')); invalid += 1
            if n == 0:
                cmd('writew 0x52000950 0'); invalid += 1
                assert int(cmd('readw 0x52000950')[0], 0) == 1
            b.write('fifo', 0x31); b.step(1000); assert b.read('fifo') == 0x94
            assert b.read('dma') == 0 and b.read('sample') == 0 and b.read(0x14) == 4
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == invalid+1, diagnostics
    assert all('unimplemented' in line for line in lines[1:]), diagnostics
    return {'instances': 2, 'word_bits': [8, 32], 'modes': [0, 1, 2, 3], 'long_transfer_words': 100,
            'fifo_depth_assumption': 8, 'peer': 'word XOR 0xa5a5a5a5', 'irqs': [65, 43],
            'shared_gate_i2c_interoperation': True, 'invalid_mmio_diagnostics': invalid,
            'silicon_verified': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--qemu', required=True)
    print(json.dumps(spi_test(p.parse_args().qemu), indent=2))
