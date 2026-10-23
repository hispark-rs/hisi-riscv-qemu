#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""ADC v155 calibration timing/errors; capture has a separate profile and test."""
import argparse
import json
from ws53_gpio_test import running
from ws53_spi_test import Bus as Spi, PROFILE as SPI_PROFILE, NOTICE as SPI_NOTICE

PROFILE = ('-global', 'ws53-adc.assume-profile=on')
NOTICE = 'ws53-adc: assumed calibration settle5us stages100/128/160us falling-edge-clear'
UNKNOWN = 'ws53-adc: unimplemented calibration without assume-profile'
BASE = 0x5208a000
PERIODS = (100000, 128000, 160000)


class Bus:
    def __init__(self, cmd): self.cmd = cmd
    def read(self, off): return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)
    def write(self, off, value): self.cmd(f'writel {BASE+off:#x} {value:#x}')
    def step(self, ns): self.cmd(f'clock_step {ns}')
    def prop(self, name): return self.cmd.qmp('qom-get', {'path': '/machine/adc', 'property': name})
    def fault(self, name, on=True, index=0): self.cmd(f'set_irq_in /machine/adc {name} {index} {int(on)}')
    def code(self, value):
        for bit in range(6): self.fault('code', (value >> bit) & 1, bit)
    def gate(self, address, bit, on=True):
        old = int(self.cmd(f'readw {address:#x}')[0], 0)
        self.cmd(f'writew {address:#x} {old | bit if on else old & ~bit:#x}')
    def pulse(self, off): self.write(off, 1); self.write(off, 0)
    def initialize(self):
        self.gate(0x52000044, 4); self.gate(0x52000548, 0x1000)
        self.cmd('writel 0x52000054 0'); self.step(1000)
        self.cmd('writel 0x52000054 0x2000'); self.step(1000)
        for off, v in ((0xf4, 0x2aa6010), (0xf8, 0x2085), (0xfc, 12), (0x24, 0x260)):
            self.write(off, v)
        self.step(5000); self.write(0x24, 0x103f8); self.write(0x24, 0x1ffff)
    def prepare(self, stage):
        if stage < 2:
            self.pulse(0x3c)
            if stage == 0:
                self.write(0x2c, 1); self.write(0x34, 0); self.pulse(0x38)
            else:
                code = self.read(0x50)
                manual = 0 if code <= 3 else code-3 if code <= 31 else code+3 if code <= 59 else 63
                self.write(0x2c, manual << 8)
        else:
            for off in (0x98, 0x9c, 0x94): self.pulse(off)
            self.write(0x88, 0x349)
    def start(self, stage):
        self.write(0x8c if stage == 2 else 0x30, 1)
        if stage == 1: self.write(0x34, 1); self.pulse(0x38)
    def status(self, stage): return self.read(0xa0 if stage == 2 else 0x44)
    def disable(self): self.write(0x30, 0); self.write(0x8c, 0)
    def complete(self, stage):
        self.prepare(stage); assert self.status(stage) == 0
        self.start(stage); self.step(PERIODS[stage]); assert self.status(stage) == 1
        self.disable()


def adc_test(qemu):
    with running(qemu, False) as (cmd, _, _, diagnostics):
        b = Bus(cmd); b.initialize(); b.prepare(0); b.start(0); b.step(1000000)
        assert b.status(0) == b.prop('starts') == 0
    assert diagnostics[0].splitlines() == [UNKNOWN], diagnostics
    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        b = Bus(cmd); cmd('irq_intercept_in /machine/intc')
        for code in range(64):
            reset(); b.code(code); b.initialize()
            for stage in range(3):
                b.prepare(stage); assert b.status(stage) == 0
                b.start(stage); b.step(PERIODS[stage]-1); assert b.status(stage) == 0
                b.step(1); assert b.status(stage) == 1
                b.disable()
            assert b.read(0x50) == code
            assert b.prop('starts') == b.prop('completions') == 3
            assert not irq.get(80, False)  # Source handler is empty; no invented IRQ.
            # Completion clear is a1->0 pulse; writes of1 alone are insufficient.
            b.write(0x3c, 1); assert b.status(0) == 1
            b.write(0x3c, 0); assert b.status(0) == 0
        # Each stage can stall, be cancelled, and be reinitialized independently.
        for stage in range(3):
            reset(); b.initialize()
            for prior in range(stage): b.complete(prior)
            b.fault('stall', True, stage); b.prepare(stage); b.start(stage); b.step(1000000)
            assert b.status(stage) == 0 and b.prop('completions') == stage
            b.disable(); b.fault('stall', False, stage); b.step(1000000)
            assert b.status(stage) == 0
            b.complete(stage); assert b.prop('completions') == stage+1
        # External source or either gate pauses exact remaining time.
        for gate in (None, (0x52000044, 4), (0x52000548, 0x1000)):
            reset(); b.initialize(); b.prepare(0); b.start(0); b.step(1234)
            if gate: b.gate(*gate, False)
            else: b.fault('clock', False)
            b.step(1000000); assert b.status(0) == 0
            if gate: b.gate(*gate)
            else: b.fault('clock')
            b.step(98765); assert b.status(0) == 0
            b.step(1); assert b.status(0) == 1
        reset(); b.initialize(); b.complete(0); b.complete(1); b.fault('cap-error')
        b.prepare(2); b.start(2); b.step(PERIODS[2]); assert b.status(2) == 0x101
        assert b.prop('failures') == 1 and b.prop('completions') == 2
        b.disable(); b.pulse(0x98); assert b.status(2) == 256
        b.pulse(0x9c); assert b.status(2) == 0
        b.fault('cap-error', False); b.complete(2)
        # Clear fault makes stale completion visible. Reset does not erase faults.
        b.fault('stale'); reset(); b.initialize(); b.prepare(0)
        assert b.status(0) == 1 and b.prop('starts') == 0
        b.fault('stale', False); b.complete(0)
        # Soft reset cancels in-progress work without late completion.
        b.prepare(1); b.start(1); b.step(1000)
        cmd('writel 0x52000054 0'); b.step(1000000)
        assert b.read(0x44) == b.read(0xa0) == b.read(0x24) == 0
        b.initialize(); b.complete(0); b.complete(1); b.complete(2)
    assert diagnostics[0].splitlines() == [NOTICE], diagnostics
    # Shared GLOBAL_CLKEN and M_CLKEN1 must preserve progress in both SPI layouts.
    with running(qemu, False, (*PROFILE, *SPI_PROFILE, '-global',
                              'ws53-global-clken.initial-value=1')) as (cmd, _, _, diagnostics):
        b = Bus(cmd); s0 = Spi(cmd,0); s1 = Spi(cmd,1)
        s0.init(); s1.init(); b.initialize(); b.prepare(0); b.start(0)
        s0.write('fifo', 0x12); s1.write('fifo', 0x34)
        b.gate(0x52000548,0x1000,False); b.step(1000)
        assert s0.read('fifo') == 0xb7 and s1.read('fifo') == 0x91 and b.status(0) == 0
        b.gate(0x52000548,0x1000); b.gate(0x52000044,0x800,False)
        b.gate(0x52000548,0x80,False); b.step(100000); assert b.status(0) == 1
        assert int(cmd('readw 0x52000548')[0],0) == 0x1001
        assert int(cmd('readw 0x52000044')[0],0) == 0x15
    assert set(diagnostics[0].splitlines()) == {NOTICE,SPI_NOTICE}, diagnostics
    with running(qemu, False, PROFILE) as (cmd, _, _, diagnostics):
        b = Bus(cmd); b.initialize()
        commands = ['readb 0x5208a024','readw 0x5208a024','readq 0x5208a024','readl 0x5208a025',
                    'writew 0x5208a024 0','readl 0x5208a038','readl 0x5208a03c','readl 0x5208a094',
                    'readl 0x5208a098','readl 0x5208a09c','readl 0x5208a0f4','writel 0x5208a044 1',
                    'writel 0x5208a050 1','writel 0x5208a0a0 1','writel 0x5208a038 2',
                    'writel 0x5208a030 2','writel 0x5208a08c 2','writel 0x5208a024 0',
                    'writel 0x5208a02c 2','writel 0x5208a034 2','writel 0x5208a088 1',
                    'writel 0x5208a0f4 0','writel 0x5208a01c 1','readl 0x5208a004',
                    'readw 0x52000054','writel 0x52000054 0x2001', 'readl 0x52000548',
                    'writew 0x52000548 0x1001', 'writel 0x52000044 0x15']
        for command in commands: cmd(command)
        b.prepare(0); b.start(0); b.write(0x2c,1); commands.append('active reconfigure')
        b.step(100000); assert b.status(0) == 1
        # A correctly valued power step is still invalid before analog settling.
        b.disable(); b.write(0x24,0x260); b.step(4999)
        b.write(0x24,0x103f8); commands.append('early analog release')
        assert b.read(0x24)==0x260
        b.step(1); b.write(0x24,0x103f8); b.write(0x24,0x1ffff)
        assert b.read(0x24)==0x1ffff
    lines = diagnostics[0].splitlines()
    assert lines[0] == NOTICE and len(lines)==len(commands)+1, diagnostics
    assert all('unimplemented' in line for line in lines[1:]), diagnostics
    return {'offset_inputs':64,'stages':3,'calibrations':192,'gate_pause_ns_exact':True,
            'shared_spi_clock_preserved':True,'faults':['three_stages','clock','stale','cap_error'],
            'invalid_diagnostics':len(commands),'capture_test':'ws53_adc_capture_test.py','hardware_verified':False}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu',required=True)
    print(json.dumps(adc_test(p.parse_args().qemu),indent=2))
