#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SDIO init/event model tests, not host protocol, CIS or ADMA transfers."""
import argparse
import json
from ws53_gpio_test import running

BASE, RESET, CLK0, CLK1 = 0x52060000, 0x570000c8, 0x52000040, 0x52000044
PROFILE = ('-global', 'ws53-sdio.assume-init-profile=on')
NOTICE = 'ws53-sdio: assumed initialization/latched-status/ACK profile; no CIS, ADMA or host protocol'


class Sdio:
    def __init__(self, command): self.cmd = command
    def read(self, off): return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)
    def write(self, off, v): self.cmd(f'writel {BASE+off:#x} {v:#x}')
    def input(self, name, n, level): self.cmd(f'set_irq_in /machine/sdio {name} {n} {int(level)}')
    def inject(self, n): self.input('inject', n, True)
    def count(self, name): return self.cmd.qmp('qom-get', {'path':'/machine/sdio', 'property':name})
    def gate(self, which, enabled):
        address, bit = (CLK0, 11) if which == 0 else (CLK1, 1)
        value = int(self.cmd(f'readl {address:#x}')[0], 0)
        self.cmd(f'writel {address:#x} {(value & ~(1<<bit)) | (int(enabled)<<bit):#x}')
    def clocks(self): self.gate(0, True); self.gate(1, True)
    def configure(self):
        self.write(4, 0xb36343)
        for off, keep, value in ((0x10c,0xff000000,0xff8000), (0x34,0xffffffff,0xfff),
                                  (0x114,0xffffffff,0x3fff), (0x118,0xffffc000,7),
                                  (0x24,0xffffffff,0x7f), (0x28,0xffffffff,0x10),
                                  (0x2c,0,0x2f92), (0x30,0xffffffff,1)):
            self.write(off, (self.read(off)&keep) | value)


def sdio_init_test(qemu):
    with running(qemu, False) as (cmd, _, _, diagnostics):
        s = Sdio(cmd); s.clocks()
        assert s.read(0x44) == 0 and s.count('rejections') == 1
    assert diagnostics[0].splitlines() == ['ws53-sdio: unimplemented/unavailable read +0x44 size=4']
    for seed in (0, 0xffff0000, 0xa55a5aa5, 0x5aa5a55a):
        options = PROFILE+('-global', f'ws53-sdio.config-seed={seed}',
                           '-global', 'ws53-mclken0.initial-value=0x5aa50000',
                           '-global', 'ws53-mclken1.initial-value=0xa55a0011')
        with running(qemu, False, options) as (cmd, irq, reset, diagnostics):
            s=Sdio(cmd); cmd('irq_intercept_in /machine/intc')
            s.clocks()
            assert int(cmd(f'readl {CLK0:#x}')[0],0) == 0x5aa50800
            assert int(cmd(f'readl {CLK1:#x}')[0],0) == 0xa55a0013
            # Both access widths observe one register; halfword writes keep the upper half.
            cmd(f'writew {CLK1:#x} 0x813')
            assert int(cmd(f'readl {CLK1:#x}')[0],0) == 0xa55a0813
            cmd(f'writew {CLK1:#x} 0x13')
            assert s.read(0x44) == 0 and not s.read(0x11c)&1
            s.input('host',0,True); assert s.read(0x44) == 1
            s.configure()
            assert s.read(0x34) == (seed|0xfff)
            assert s.read(0x10c) == ((seed&0xff000000)|0xff8000)
            assert s.read(0x118) == ((seed&0xffffc000)|7)
            s.input('host',1,True)
            assert s.count('host-enables') == 1 and s.read(0x110) == 0x2000
            assert s.read(0x1c) == 4 and not irq.get(46,False)
            s.write(0x18, (seed&~7)|4); s.read(0x18)
            assert irq.get(46,False)
            s.write(0x1c,4)
            assert s.read(0x1c) == 4, 'global ACK cannot consume child status'
            s.inject(9)
            cmd.qmp('qom-set', {'path':'/machine/sdio','property':'host-message','value':0x12345678})
            assert s.read(0x128) == 0x12345678
            s.write(0x110,0x2000)
            assert s.read(0x110) == 0x200 and irq.get(46,False), 'later event was lost'
            s.write(0x110,0x200)
            assert s.read(0x110) == 0 and not irq.get(46,False)
            s.write(0x11c,s.read(0x11c)|1)
            assert s.read(0x11c)&1
            # Independent masks: raw child status survives global masking and gate loss.
            s.write(0x18,seed&~7); s.inject(10)
            assert s.read(0x110) == 0x400 and not irq.get(46,False)
            s.gate(0,False); s.gate(0,True)
            assert s.read(0x110) == 0x400
            s.write(0x18,(seed&~7)|4); s.read(0x18)
            assert irq.get(46,False)
            # Soft reset only models initial/event state; no ADMA drain claim.
            control=int(cmd(f'readl {RESET:#x}')[0],0)
            cmd(f'writel {RESET:#x} {control&~2:#x}')
            cmd(f'writel {RESET:#x} {control|2:#x}')
            assert s.count('resets') == 1 and s.read(0x110) == 0 and not s.read(0x11c)&1
            assert not irq.get(46,False) and s.count('host-enables') == 0
            s.configure(); s.input('host',1,True)
            assert s.read(0x110) == 0x2000
            assert s.count('rejections') == 0
            reset()
            s.clocks()
            assert s.read(0x110) == 0 and s.count('host-enables') == s.count('resets') == 0
        assert diagnostics[0].splitlines() == [NOTICE], diagnostics

    invalid = []
    with running(qemu,False,PROFILE) as (cmd,_,_,diagnostics):
        s=Sdio(cmd); s.clocks()
        # Missing clock cannot silently admit profile writes or host enable.
        s.write(4,0xb36343); invalid.append('clockless profile')
        s.input('host',1,True); invalid.append('premature host enable')
        s.input('host',0,True); s.configure()
        # Direct READY without a corresponding host event is rejected.
        s.write(0x11c,1); invalid.append('stale ready')
        s.input('host',1,True); s.input('fault',0,True)
        s.write(0x11c,1); assert s.read(0x11c)==0
        s.input('fault',0,False); s.write(0x11c,1); assert s.read(0x11c)==1
        for command in (f'readl {BASE+4:#x}', f'writel {BASE+0x40:#x} 0x20010000',
                        f'writel {BASE+0x104:#x} 1', f'writel {BASE+0x120:#x} 4',
                        f'writel {BASE+0x12c:#x} 1', f'readl {BASE+0x254:#x}',
                        f'readw {BASE+0x44:#x}', f'readb {BASE+0x44:#x}',
                        f'readq {BASE+0x44:#x}', f'readl {BASE+0x45:#x}',
                        f'writel {BASE+0x28:#x} 0x11', f'writel {BASE+0x34:#x} 0x10fff',
                        f'readw {RESET:#x}', f'writel {RESET:#x} 0x102'):
            cmd(command); invalid.append(command)
        s.gate(1,False); assert s.read(0x44)==0; invalid.append('AHB gate off')
        s.gate(1,True); assert s.read(0x110)==0x2000
        assert s.count('rejections')==len(invalid)
    assert diagnostics[0].splitlines()[0]==NOTICE
    assert len(diagnostics[0].splitlines())==1+len(invalid), diagnostics
    return dict(config_seeds=4, explicit_profile_required=True, word_halfword_aliases=True,
                host_clock_and_fresh_enable=True, irq46_mask_and_ack=True,
                later_event_survives_ack=True, gate_preserves_pending=True,
                reset_reinitialize=True, rejected_accesses=len(invalid),
                hardware_tested=False, host_protocol_or_adma=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(sdio_init_test(parser.parse_args().qemu),indent=2))
