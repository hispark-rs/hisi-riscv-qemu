#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""KM state/data tests; explicit virtual completion/ownership assumptions only."""
import argparse
import json
from pathlib import Path
from ws53_gpio_test import running

BASE = 0x52022000
PROFILE = ('-global', 'ws53-km.assume-profile=on', '-global', 'ws53-km.route-clear-irq=on')
NOTICE = 'ws53-km: assumed slot/clear-loader profile; no silicon ownership or erasure proof'


class Km:
    def __init__(self, command): self.cmd = command
    def read(self, off): return int(self.cmd(f'readl {BASE+off:#x}')[0], 0)
    def write(self, off, value): self.cmd(f'writel {BASE+off:#x} {value:#x}')
    def step(self, ns=5000): self.cmd(f'clock_step {ns}')
    def get(self, name): return self.cmd.qmp('qom-get', dict(path='/machine/km', property=name))
    def inject(self, name, value): self.cmd.qmp('qom-set', dict(path='/machine/km', property='inject-'+name, value=value))
    def fault(self, number, enabled): self.cmd(f'set_irq_in /machine/km fault {number} {int(enabled)}')
    def clock(self, enabled): self.cmd(f'set_irq_in /machine/km clock 0 {int(enabled)}')
    def keys(self): return [bytes.fromhex(x) for x in self.get('debug-keys').split(':')]
    def slots(self): return [tuple(bytes.fromhex(x)) for x in self.get('debug-slots').split(':')]
    def staging(self): return bytes.fromhex(self.get('debug-staging'))
    @staticmethod
    def selector(slot): return slot if slot < 8 else 0x8000 | (slot-8)
    def select(self, slot): self.write(0x1b30, self.selector(slot))
    def claim(self, slot, owner=1):
        self.write(0x1b04 if owner == 1 else 0x1b00, self.selector(slot) | 0x100000)
        self.select(slot)
    def release(self, slot, owner=1):
        self.write(0x1b04 if owner == 1 else 0x1b00, self.selector(slot))
    def configure(self, engine=0x20, usage=3):
        self.write(0x1014, 1 | (engine << 4) | (usage << 16))
        self.write(0x1018, 10)
    def stage(self, slot, data, *, block=0, size=1):
        assert len(data) == 16
        self.write(0x1010, slot*2+block if slot < 8 else slot-8)
        for i in range(4): self.write(0x1000+i*4, int.from_bytes(data[i*4:i*4+4], 'little'))
        self.write(0x1438, 1 | (size << 2) if slot < 8 else 1 | (block << 13))


def km_test(qemu):
    report = dict(cipher_keys=0, hmac_keys=0, completed_blocks=0, ownership_checks=0,
                  faults=0, invalid_accesses=0, reset_cases=0)
    with running(qemu, False) as (cmd, _, _, diagnostics):
        k = Km(cmd)
        assert k.read(0x1b30) == 0 and k.get('rejections') == 1
    assert diagnostics[0].splitlines() == ['ws53-km: unimplemented/unavailable read +0x1b30']

    with running(qemu, False, PROFILE) as (cmd, irq, reset, diagnostics):
        k = Km(cmd); cmd('irq_intercept_in /machine/intc')
        expected = [bytearray(128) for _ in range(10)]
        cases = []
        for slot in range(8):
            for engine, length in ((0x20,16), (0x20,24), (0x20,32), (0x50,16)):
                for usage in (1,2,3):
                    for parity in ((0,1) if length == 16 else (0,)):
                        cases.append((slot,engine,length,usage,parity,length))
        for slot in (8,9):
            # SHA224/256 share engine a1, SHA384/512 also use a1 but eight blocks.
            for engine, maximum in ((0xa0,64),(0xa1,64),(0xa1,64),(0xa1,128),(0xa1,128),(0xa2,64)):
                for length in (0,1,maximum): cases.append((slot,engine,length,1,0,maximum))
        for slot,engine,length,usage,parity,maximum in cases:
            k.claim(slot); assert k.read(0x1b34) == 1
            k.configure(engine,usage)
            key = bytes((i*17+slot+length+3)&255 for i in range(length))
            padded = key + bytes((maximum+15)//16*16-length)
            before = k.get('completed-blocks')
            for block in range(len(padded)//16):
                half = parity if length == 16 and slot < 8 else block
                k.stage(slot,padded[block*16:block*16+16],block=half,size={16:1,24:2,32:3}.get(length,0))
                assert k.read(0x1438) & 1
                k.step(4999); assert k.read(0x1438) & 1
                k.step(1); assert not k.read(0x1438) & 1
                assert k.read(0x1044) & 0x4000 and irq[61]
                k.write(0x1044, k.read(0x1044) | 0x4000)
                assert not k.read(0x1044) & 0x4000 and not irq[61]
                expected[slot][half*16:half*16+16] = padded[block*16:block*16+16]
            assert k.get('completed-blocks')-before == len(padded)//16
            report['completed_blocks'] += len(padded)//16
            assert k.keys() == [bytes(x) for x in expected]
            assert k.slots()[slot][0] == 1 and k.slots()[slot][1] != 0
            for i in range(4): k.write(0x1000+i*4, 0)
            assert k.staging() == bytes(16) and k.keys() == [bytes(x) for x in expected]
            k.release(slot); assert k.read(0x1b14) == 1 and k.read(0x1b34) == 1
            k.step(4999); assert k.read(0x1b14) == 1
            k.step(1); assert k.read(0x1b14) == k.read(0x1b34) == 0
            assert k.slots()[slot][1] == 0 and k.keys() == [bytes(x) for x in expected]
            report['cipher_keys' if slot < 8 else 'hmac_keys'] += 1
        # Atomic claim does not steal from either CPU, wrong-owner flush fails.
        for slot in range(10):
            for owner in (1,2):
                k.claim(slot,owner); k.claim(slot,3-owner)
                assert k.read(0x1b34) == owner
                k.release(slot,3-owner)
                assert k.read(0x1b14 if owner == 2 else 0x1b10) == 2
                k.release(slot,owner); k.step(); assert k.read(0x1b34) == 0
                report['ownership_checks'] += 1
        # Reset cannot publish a late key/flush event in this explicit model.
        for flushing in (False,True):
            reset(); k.claim(0); k.configure(); k.stage(0,b'K'*16)
            if flushing: k.step(); k.release(0)
            reset(); k.step(50000)
            assert k.keys() == [bytes(128)]*10 and k.slots() == [(0,0,0,0)]*10
            assert k.get('completed-blocks') == k.get('released-slots') == 0
            assert not irq[61]; report['reset_cases'] += 1
    assert diagnostics[0].splitlines() == [NOTICE],diagnostics

    for fault in range(8):
        with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
            k=Km(cmd); k.fault(fault,True); k.claim(0)
            if fault == 1:
                assert k.read(0x1b34) == 0
                k.fault(fault,False); k.claim(0); assert k.read(0x1b34) == 1
            else:
                k.configure(); k.stage(0,b'Q'*16); k.step(50000)
                if fault == 0:
                    assert k.read(0x1438)&1 and k.keys()[0] == bytes(128)
                    k.fault(fault,False); k.step(); assert not k.read(0x1438)&1
                elif fault == 2:
                    assert k.read(0x1b34) == 2 and k.keys()[0] == bytes(128)
                elif fault == 3:
                    k.write(0x1044,0x4000); assert k.read(0x1044)&0x4000
                    k.fault(fault,False); k.write(0x1044,0x4000); assert not k.read(0x1044)&0x4000
                else:
                    k.release(0); k.step(50000)
                    assert k.read(0x1b14) == {4:1,5:4,6:2,7:0}[fault]
                    assert k.read(0x1b34) == 1 and k.keys()[0][:16] == b'Q'*16
                    k.fault(fault,False)
                    if fault != 4: k.release(0)
                    k.step(); assert k.read(0x1b34) == 0
            report['faults'] += 1
        assert diagnostics[0].splitlines() == [NOTICE],diagnostics

    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        k=Km(cmd); k.claim(0); k.configure(); k.stage(0,b'C'*16)
        k.clock(False); k.step(50000)
        assert k.keys()[0] == bytes(128) and k.get('completed-blocks') == 0
        k.clock(True); k.step(); assert k.keys()[0][:16] == b'C'*16
        k.release(0); k.clock(False); k.step(50000); assert k.slots()[0][0] == 1
        k.clock(True); k.step(); assert k.slots()[0][0] == 0
        report['clock_paused_load_and_flush'] = True
        # Malformed/unimplemented accesses are diagnosed, not silent storage.
        for text in (f'readb {BASE+0x1010:#x}', f'readq {BASE+0x1010:#x}',
                     f'readl {BASE+0x1000:#x}', f'readl {BASE+0x107c:#x}',
                     f'writel {BASE+0x1038:#x} 1', f'writel {BASE+0x1074:#x} 1',
                     f'writel {BASE+0x1b30:#x} 8', f'writel {BASE+0x1b30:#x} 0x8002',
                     f'writel {BASE+0x1b30:#x} 0x10000', f'writel {BASE+0x1044:#x} 1'):
            before=k.get('rejections'); cmd(text); assert k.get('rejections')==before+1
            report['invalid_accesses']+=1
        # Inherited errors prevent publication. Unknown command encodings never
        # become apparent successes and no root/common/Flash route is simulated.
        k.claim(0); k.configure(); k.inject('loader-error',0x80000000)
        k.stage(0,b'E'*16); assert k.read(0x1038)==0x80000000 and k.get('completed-blocks')==1
        k.inject('loader-error',0); k.stage(0,b'R'*16); k.step()
        assert k.keys()[0][:16]==b'R'*16
        report['invalid_accesses']+=1
    assert len(diagnostics[0].splitlines()) == 1+report['invalid_accesses'],diagnostics
    assert all('ws53-km: unimplemented/unavailable' in line for line in diagnostics[0].splitlines()[1:])
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        k=Km(cmd)
        malformed=(
            {'owner':0}, {'owner':2}, {'words':3}, {'address':16},
            {'engine':0x10}, {'engine':0x50,'control':9}, {'control':1},
            {'port':7}, {'security':0}, {'control':4},
            {'engine':0xa1,'address':2,'control':1},
            {'engine':0xa0,'control':0x8001}, {'engine':0xa1,'control':5},
        )
        for change in malformed:
            reset()
            hmac=change.get('engine',0x20)>=0xa0
            slot=8 if hmac else 0
            owner=change.get('owner',1)
            if owner: k.claim(slot,owner)
            k.configure(change.get('engine',0x20))
            k.write(0x1014,change.get('port',1)|(change.get('engine',0x20)<<4)|0x30000)
            k.write(0x1018,change.get('security',10)); k.write(0x1010,change.get('address',0))
            for word in range(change.get('words',4)): k.write(0x1000+word*4,0x51515151)
            k.write(0x1438,change.get('control',5)); k.step()
            assert k.get('rejections')==1 and k.get('submitted-blocks')==0,change
            assert k.keys()==[bytes(128)]*10 and k.read(0x1044)==0
        reset(); k.claim(0); k.configure(); k.stage(0,b'B'*16)
        for off,value in ((0x1000,0),(0x1010,2),(0x1014,7),(0x1018,0),(0x1438,5)):
            before=k.get('rejections'); k.write(off,value); assert k.get('rejections')==before+1
        k.step(); assert k.keys()[0][:16]==k.staging()==b'B'*16
        assert k.read(0x1010)==0 and k.read(0x1014)==0x30201 and k.read(0x1018)==10
        k.clock(False); k.write(0x1010,2); assert k.read(0x1010)==0
        k.clock(True); assert k.read(0x1010)==0
        report['malformed_submissions']=len(malformed)
        report['busy_or_clock_access_rejections']=7
    assert len(diagnostics[0].splitlines())==1+len(malformed)+7,diagnostics
    report['passed']=True
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    report=km_test(args.qemu)
    if args.output: args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
