#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 I2S source-derived MMIO with explicit, unverified digital assumptions."""
import argparse
import json
from ws53_gpio_test import running
from ws53_pwm_test import access as pwm_access, PROFILE as PWM_PROFILE, NOTICE as PWM_NOTICE

PROFILE = ('-global', 'ws53-i2s.assume-profile=on')
NOTICE = 'ws53-i2s: assumed stereo profile FIFO8 master32MHz separate-serializer XOR-peer'
UNKNOWN = 'ws53-i2s: unimplemented transfer without assume-profile'
BASE = 0x52030000
TX, RX, INTR, RSTN = 0x1000, 0x2000, 0x4000, 0x8000
WIDTHS = (16, 18, 20, 24, 32)


class Bus:
    def __init__(self, cmd):
        self.cmd = cmd

    def read(self, off):
        return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)

    def write(self, off, value):
        self.cmd(f'writel {BASE+off:#x} {value:#x}')

    def step(self, ns):
        self.cmd(f'clock_step {ns}')

    def prop(self, name):
        return self.cmd.qmp('qom-get', {'path': '/machine/i2s', 'property': name})

    def fault(self, name, on=True):
        self.cmd(f'set_irq_in /machine/i2s {name} 0 {int(on)}')

    def gate(self, on=True):
        old = int(self.cmd('readl 0x52000040')[0], 0)
        self.cmd(f'writel 0x52000040 {(old | 4) if on else (old & ~4):#x}')

    def init(self, width=16, master=True, edge=True, div=25):
        self.cmd('writel 0x57008600 1'); self.gate()
        self.write(0x60, 0xff00); self.write(0x90, 0)
        self.write(0x40, (128 if master else 0) | (64 if edge else 0))
        code = WIDTHS.index(width) + 1
        self.write(0x78, code | code << 3)
        if master:
            self.write(0x94, div); self.write(0x98, 2*width); self.write(0x9c, width)
        for off in (0x3c, 0x7c, 0x80, 0x84, 0x88): self.write(off, 0)
        self.write(0x64, 0x704); self.write(0x5c, RSTN | INTR)
        self.write(0x48, 63); self.write(0x8c, 3); self.write(0x90, int(master))
        return (div*width*2*1_000_000_000 + 31_999_999)//32_000_000 if master else 100000

    def push(self, left, right):
        self.write(0x4c, left); self.write(0x50, right)

    def pop(self):
        return self.read(0x54), self.read(0x58)

    def start(self, directions=TX | RX):
        self.write(0x90, self.read(0x90) | 2)
        self.write(0x5c, directions)

    def stop(self):
        self.write(0x60, TX | RX)


def i2s_test(qemu):
    with running(qemu, False) as (cmd, _, _, diagnostics):
        b = Bus(cmd); b.init(); b.push(1, 2); b.start(); b.step(100000)
        assert b.prop('frames') == 0 and b.read(0x6c) == 0x101
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        cmd('irq_intercept_in /machine/intc')
        b = Bus(cmd)
        for width in WIDTHS:
            for master in (True, False):
                for edge in (True, False):
                    reset(); period = b.init(width, master, edge)
                    mask = (1 << width) - 1
                    b.push(mask, 0x1234); b.start()
                    # Serializer consumes one pair now but wire completion is later.
                    assert b.read(0x6c) == 0 and b.prop('in-flight') == 1
                    assert b.prop('transmitted') == 0 and b.read(0x68) == 0
                    b.step(period-1); assert b.read(0x68) == 0
                    b.push(0x2345, mask)
                    b.step(1); assert b.pop() == (mask ^ 0xa5a5, 0x1234 ^ 0x5a5a)
                    assert b.prop('last-left') == mask and b.prop('last-right') == 0x1234
                    # Continuous stream with data-dependent virtual responses.
                    for n in range(100):
                        b.push(n, n + 1)
                        b.step(period)
                        expected = (0x2345, mask) if n == 0 else (n-1, n)
                        assert b.pop() == (expected[0] ^ 0xa5a5, expected[1] ^ 0x5a5a)
                        assert b.read(0x44) & 60 == 0 and not irq.get(67, False)
                    assert b.prop('transmitted') == b.prop('received') == 101
                    b.stop(); b.step(period*2); assert b.prop('frames') == 101
                    assert b.prop('in-flight') == 0
        # Divider endpoints and rounding must change completion time, not just readback.
        for div in (1, 127):
            reset(); period = b.init(18, div=div); b.start(RX)
            b.step(period-1); assert b.prop('frames') == 0
            b.step(1); assert b.pop() == (1, (1 << 18)-2)
        # TX threshold is live, independently masked and level-reasserted after W1C.
        reset(); period = b.init(); b.fault('stall')
        for n in range(8): b.push(n, n)
        b.start(TX); b.write(0x8c, 61); assert not irq.get(67, False)
        b.fault('stall', False); b.step(period*3)
        assert b.read(0x6c) == 0x404 and irq.get(67, False)
        b.write(0x48, 2); assert irq.get(67, False)
        b.push(9, 9); assert not irq.get(67, False)
        b.stop()
        # A short RX burst below SDK RX threshold must still be readable.
        reset(); period = b.init(); b.start(RX); b.step(period); b.stop()
        assert b.read(0x68) == 0x101 and b.read(0x44) & 1 == 0
        assert b.pop() == (1, 0xfffe)
        # Independent overflows, W1C, inverted mask and global interrupt enable.
        b.start(RX); b.step(period*8)
        assert b.read(0x68) == 0x808 and b.read(0x44) & 1
        assert not irq.get(67, False)  # RX threshold still masked
        b.write(0x8c, 62); assert irq.get(67, False)
        b.write(0x48, 1); assert irq.get(67, False)  # threshold is a level
        b.write(0x8c, 3); assert not irq.get(67, False)
        b.read(0x54); b.step(period)
        assert b.read(0x44) & 12 == 4 and irq.get(67, False)
        b.write(0x60, INTR); assert not irq.get(67, False)
        b.write(0x5c, INTR); assert irq.get(67, False)
        b.write(0x48, 4); assert not irq.get(67, False)
        b.read(0x58); b.step(period)
        assert b.read(0x44) & 12 == 8
        b.write(0x48, 8); b.step(period); b.stop()
        assert b.read(0x44) & 12 == 12
        b.write(0x48, 12); assert not irq.get(67, False)
        # Missing TX right/left channels report distinct errors, not fabricated success.
        reset(); period = b.init(); b.write(0x4c, 1); b.start(TX)
        assert b.read(0x44) & 48 == 16 and irq.get(67, False)
        b.write(0x48, 16); b.write(0x50, 2); b.step(period)
        assert b.read(0x44) & 48 == 32
        b.write(0x48, 32); b.step(period); b.stop()
        assert b.read(0x44) & 48 == 48
        # FIFO can contain eight pairs in addition to one in-flight serializer.
        reset(); period = b.init(); b.push(1, 2); b.start()
        for n in range(8): b.push(n, n+1)
        assert b.read(0x6c) == 0x808
        b.fault('stall'); b.step(period*10)
        assert b.read(0x6c) == 0x808 and b.prop('frames') == 0
        b.fault('stall', False); b.step(period); assert b.read(0x6c) == 0x707
        # Source clock, upstream gate, CMU and internal gate preserve remaining time.
        for kind in ('clock', 'gate', 'cmu', 'internal'):
            reset(); period = b.init(); b.push(3, 4); b.push(5, 6); b.start()
            b.step(123)
            if kind == 'clock': b.fault('clock', False)
            elif kind == 'gate': b.gate(False)
            elif kind == 'cmu': cmd('writel 0x57008600 0')
            else: b.write(0x90, 1)
            b.step(period*4); assert b.prop('frames') == 0
            if kind == 'clock': b.fault('clock')
            elif kind == 'gate': b.gate()
            elif kind == 'cmu': cmd('writel 0x57008600 1')
            else: b.write(0x90, 3)
            b.step(period-124); assert b.prop('frames') == 0
            b.step(1); assert b.pop() == (3 ^ 0xa5a5, 4 ^ 0x5a5a)
        # Slave has independent peer clock, unaffected by master divider.
        reset(); period = b.init(master=False); b.start(RX); b.step(99)
        b.fault('slave-clock', False); b.step(period*2); assert b.prop('frames') == 0
        b.fault('slave-clock'); b.step(period-99); assert b.prop('frames') == 1
        # Reset-stall exposes unsuccessful cleanup instead of auto-passing recovery.
        b.fault('reset-stall'); b.write(0x60, RSTN | TX | RX)
        assert b.read(0x68) == 0x101
        b.fault('reset-stall', False); b.write(0x60, RSTN)
        assert b.read(0x68) == 0 and b.read(0x78) == 9
        # System reset cancels serializer and clears queues but external faults survive.
        b.init(); b.push(1, 2); b.start(); b.fault('clock', False); reset()
        b.init(); b.push(1, 2); b.start(); b.step(period*2)
        assert b.prop('frames') == 0 and b.read(0x6c) == 0x101
        b.fault('clock'); b.step(25000); assert b.prop('frames') == 1
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    # Shared halfword/word alias and simultaneous PWM/I2S progress.
    inherited = 0x12340001  # Opaque inherited state, not implemented peripherals.
    with running(qemu, False, (*PROFILE, *PWM_PROFILE, '-global',
                              f'ws53-mclken0.initial-value={inherited}')) as (cmd, _, reset, diagnostics):
        b = Bus(cmd); period = b.init(); b.start(RX)
        assert int(cmd('readl 0x52000040')[0], 0) == inherited | 4
        cmd('writew 0x52000040 0x85'); cmd('writew 0x5200007c 0x41'); cmd('writew 0x5200007c 0x43')
        read, _, _, configure, commit, _ = pwm_access(cmd)
        configure(0, cycles=0); commit(0, 1)
        assert int(cmd('readl 0x52000040')[0], 0) == inherited | 0x84
        b.gate(False); b.step(1000); assert read(0x12c) == 1 and b.prop('frames') == 0
        b.gate(); cmd('writew 0x52000040 5')
        b.step(period); assert read(0x12c) == 1 and b.prop('frames') == 1
        reset(); assert int(cmd('readl 0x52000040')[0], 0) == inherited
    assert set(diagnostics[0].splitlines()) == {NOTICE, PWM_NOTICE}, diagnostics

    invalid = []
    with running(qemu, False, PROFILE) as (cmd, _, reset, diagnostics):
        b = Bus(cmd); b.init()
        invalid = ['readb 0x52030040', 'readw 0x52030040', 'readq 0x52030040',
                   'readl 0x52030041', 'writew 0x52030040 0', 'readl 0x5203005c',
                   'readl 0x52030060', 'readl 0x52030048', 'readl 0x5203004c',
                   'writel 0x52030044 1', 'writel 0x52030068 1', 'readl 0x520300a0',
                   'writel 0x520300c0 1', 'writel 0x52030040 1', 'writel 0x5203003c 256',
                   'writel 0x52030078 0', 'writel 0x52030078 10', 'writel 0x52030078 54',
                   'writel 0x52030090 4', 'writel 0x5203005c 256', 'writel 0x5203005c 1',
                   'writel 0x52030064 8', 'writel 0x5203008c 64', 'writel 0x52030048 64',
                   'writel 0x52030094 128', 'writel 0x52030094 0', 'writel 0x52030098 1024',
                   'writel 0x5203009c 2048', 'writel 0x52030084 1', 'writel 0x52030088 1',
                   'writel 0x5203007c 1', 'writel 0x52030080 1', 'readl 0x52030054',
                   'writel 0x5203004c 65536', 'readw 0x57008600', 'writel 0x57008600 2']
        for command in invalid: cmd(command)
        b.push(1, 2); b.start()
        b.write(0x40, 0xc0); invalid.append('active reconfigure')
        for n in range(8): b.push(n, n)
        b.write(0x4c, 1); invalid.append('full FIFO')
        b.step(25000); assert b.pop() == (1 ^ 0xa5a5, 2 ^ 0x5a5a)
        # Individually legal divider fields can still describe an unsupported
        # frame format. Reject start atomically instead of transmitting nonsense.
        reset(); b.init(); b.write(0x98, 31); b.push(1, 2); b.start()
        invalid.append('unsupported frame divider combination')
        b.step(100000); assert b.prop('frames') == 0 and b.read(0x6c) == 0x101
        b.write(0x98, 32); b.start(); b.step(24999)
        assert b.prop('frames') == 0
        b.step(1); assert b.pop() == (1 ^ 0xa5a5, 2 ^ 0x5a5a)
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines) == len(invalid)+1, diagnostics
    assert all('unimplemented' in line for line in lines[1:]), diagnostics
    return {'widths': WIDTHS, 'master_slave': True, 'edges': ['rising', 'falling'],
            'continuous_frames_per_case': 101, 'fifo_depth_assumption': 8,
            'separate_serializer': True, 'irq': 67, 'shared_pwm_clock_alias': True,
            'invalid_mmio_diagnostics': len(invalid), 'silicon_verified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu', required=True)
    print(json.dumps(i2s_test(parser.parse_args().qemu), indent=2))
