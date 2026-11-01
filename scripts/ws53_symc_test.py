#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["gmssl==3.2.2", "pycryptodomex==3.23.0"]
# ///
"""SYMC real ciphertext/IV, KM permissions, rings, IRQ59 and retained bus I/O."""
import argparse
import json
from pathlib import Path
import struct
from ws53_gpio_test import running
from ws53_km_test import Km, NOTICE as KM_NOTICE
from ws53_symc_reference import reference, known_answers, MODES

BASE = 0x52010000
PROFILE = ('-global','ws53-km.assume-profile=on','-global','ws53-symc.assume-profile=on')
NOTICE = 'ws53-symc: assumed full-block REE/ring/ACK/DMA profile; no silicon handoff or physical drain proof'
NOTICES = [KM_NOTICE, NOTICE]


class Symc:
    def __init__(self, cmd): self.cmd=cmd; self.km=Km(cmd)
    def read(self, off): return int(self.cmd(f'readl {BASE+off:#x}')[0],0)
    def write(self, off, value): self.cmd(f'writel {BASE+off:#x} {value:#x}')
    def step(self, ns=1000): self.cmd(f'clock_step {ns}')
    def get(self, name): return self.cmd.qmp('qom-get',dict(path='/machine/symc',property=name))
    def fault(self,ch,n,on=True): self.cmd(f'set_irq_in /machine/symc fault {(ch-1)*9+n} {int(on)}')
    def clock(self,on): self.cmd(f'set_irq_in /machine/symc clock 0 {int(on)}')
    def memory(self,a,data): self.cmd(f'write {a:#x} {len(data)} 0x{data.hex()}')
    def bytes(self,a,n): return bytes.fromhex(self.cmd(f'read {a:#x} {n}')[0].removeprefix('0x'))
    @staticmethod
    def addresses(ch): return 0x20050000+(ch-1)*0x2000,0x20051000+(ch-1)*0x2000,0x2005e000+(ch-1)*0x100
    @staticmethod
    def bank(ch,output=False): return (0xcf00 if output else 0x4000)+ch*0x100
    def load(self,slot,algorithm,key,parity=0,usage=3):
        k=self.km; k.claim(slot); assert k.read(0x1b34)==1
        k.configure(algorithm*16,usage)
        padded=key+bytes((-len(key))%16)
        for block in range(len(padded)//16):
            k.stage(slot,padded[block*16:block*16+16],block=parity if len(key)==16 else block,size=(len(key)-8)//8)
            k.step(); assert not k.read(0x1438)&1 and k.read(0x1044)&0x4000
            k.write(0x1044,0x4000)
    def configure(self,ch,slot,algorithm,size,mode,decrypt=False):
        bit=1<<ch
        self.write(0x20,self.read(0x20)|(1<<(4*ch)))
        assert (self.read(0x20)>>(4*ch))&15==1
        self.write(8,bit); self.write(0x60,bit); self.step(5000)
        assert self.read(8)&bit; self.write(8,bit); assert not self.read(8)&bit
        self.write(0x8610,bit); self.write(0xc000,bit)
        self.write(0x8614,self.read(0x8614)|bit); self.write(0xc004,self.read(0xc004)|bit)
        self.write(4,1)
        i,o=self.bank(ch),self.bank(ch,True)
        self.write(i,0x8000aa00); self.write(i+4,0)
        self.write(i+0x10,slot|(algorithm<<16)|(mode<<20)|(size<<24)|(int(decrypt)<<28))
        table=self.addresses(ch)[2]
        for bank,address,depth in [(i,table,0x2c),(o,table+64,0x28)]:
            self.write(bank+0x20,0); self.write(bank+0x24,address); self.write(bank+depth,2)
    def descriptors(self,ch,data,iv,parity=0):
        source,dest,table=self.addresses(ch)
        self.memory(source-32,b'G'*32); self.memory(source,data); self.memory(source+len(data),b'H'*32)
        self.memory(dest-32,b'I'*32); self.memory(dest,b'X'*len(data)); self.memory(dest+len(data),b'J'*32)
        descriptors=struct.pack('<4I',3|(parity<<9),len(data),source,0)+iv+bytes(32)+struct.pack('<4I',0,len(data),dest,0)+bytes(16)
        self.memory(table,descriptors)
        return descriptors
    def submit(self,ch):
        self.write(self.bank(ch,True)+0x30,1); self.write(self.bank(ch)+0x30,1)
    def iv(self,ch): return b''.join(self.read(0x9000+ch*0x80+i*4).to_bytes(4,'little') for i in range(4))
    def verify(self,ch,data,descriptors,expected,iv):
        source,dest,table=self.addresses(ch); bit=1<<ch
        assert self.read(0xc000)&bit and self.read(0x8610)&bit
        assert [self.read(self.bank(ch,o)+off)&255 for o in (False,True) for off in (0x30,0x34)]==[1]*4
        assert self.bytes(dest,len(data))==expected
        assert self.iv(ch)==iv
        assert self.bytes(source,len(data))==data and self.bytes(table,96)==descriptors
        for a,b in [(source-32,b'G'),(source+len(data),b'H'),(dest-32,b'I'),(dest+len(data),b'J')]: assert self.bytes(a,32)==b*32


def symc_test(qemu):
    report=dict(known_answers=0,normal=0,faults=0,late_io=0,invalid=0)
    vectors=known_answers()
    with running(qemu,False) as (cmd,_,_,diagnostics):
        s=Symc(cmd); assert s.read(0x20)==0 and s.get('rejections')==1
    assert diagnostics[0].splitlines()==['ws53-symc: unimplemented/unavailable read +0x20']
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        s=Symc(cmd); cmd('irq_intercept_in /machine/intc')
        cases=[]
        for ch in (1,2):
            for algorithm,key,mode,plain,iv,cipher in vectors:
                for dec in (False,True):
                    cases.append((True,ch,0,algorithm,key,mode,cipher if dec else plain,iv,dec,0))
            for slot in range(8):
                for algorithm,length in [(2,16),(2,24),(2,32),(5,16)]:
                    for parity in ((0,1) if length==16 else (0,)):
                        for mode in MODES:
                            for dec in (False,True):
                                key=bytes((i*13+slot*7+ch)&255 for i in range(length))
                                data=bytes((i*17+mode+slot)&255 for i in range(16*(1+(slot+mode+ch)%4)))
                                iv=bytes.fromhex('fffffffffffffffffffffffffffffffe') if mode==6 else bytes((i*3+slot)&255 for i in range(16))
                                cases.append((False,ch,slot,algorithm,key,mode,data,iv,dec,parity))
        for kat,ch,slot,algorithm,key,mode,data,iv,dec,parity in cases:
            s.load(slot,algorithm,key,parity,usage=2 if dec else 1)
            s.configure(ch,slot,algorithm,(len(key)-8)//8,mode,dec)
            descriptors=s.descriptors(ch,data,iv,parity)
            expected,final=reference(algorithm,key,mode,data,iv,dec)
            s.submit(ch); s.step(100000)
            s.verify(ch,data,descriptors,expected,final)
            assert irq[59]
            s.write(0xc000,1<<ch); assert irq[59] # input cause still held
            s.write(0x8610,1<<ch); assert not irq[59]
            s.km.release(slot); s.km.step(); assert s.km.read(0x1b34)==0
            report['known_answers' if kat else 'normal']+=1
        assert s.get('transfers')==len(cases) and s.get('rejections')==0
    assert diagnostics[0].splitlines()==NOTICES, diagnostics

    for ch in (1,2):
        for number in range(9):
            with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
                s=Symc(cmd); cmd('irq_intercept_in /machine/intc')
                s.load(0,2,b'K'*16)
                if number==5:
                    s.fault(ch,number); s.write(0x20,1<<(4*ch)); assert not s.read(0x20)
                elif number==0:
                    s.write(0x20,1<<(4*ch)); s.fault(ch,number); s.write(0x60,1<<ch); s.step(50000); assert not s.read(8)
                    s.fault(ch,number,False); s.step(5000); assert s.read(8)&(1<<ch)
                else:
                    s.configure(ch,0,2,1,3); s.descriptors(ch,b'A'*32,bytes(16));
                    if number in (1,4,6): s.fault(ch,number)
                    if number==6:
                        s.submit(ch) # lost owner rejects publication
                    else:
                        s.submit(ch)
                        if number in (2,8): s.step(1000); s.fault(ch,number)
                        if number==3: s.step(3000); assert s.get('pending-writes')==1; s.fault(ch,number)
                        s.step(50000)
                    if number in (1,2,3,4,6,8): assert not s.read(0xc000)&(1<<ch)
                    if number==1:
                        s.fault(ch,number,False); s.step(100000); assert s.read(0xc000)&(1<<ch)
                    if number==4: assert s.read(s.bank(ch,True)+0x34)==1 and s.get('bytes-written')==32
                    if number==7:
                        assert irq[59]; s.fault(ch,number); s.write(0xc000,1<<ch); assert s.read(0xc000)&(1<<ch)
                        s.fault(ch,number,False); s.write(0xc000,1<<ch); assert not s.read(0xc000)&(1<<ch)
                    if number==8:
                        assert s.get('pending-reads')==1; s.fault(ch,number,False); s.step(100000); assert s.read(0xc000)&(1<<ch)
                report['faults']+=1
            messages=diagnostics[0].splitlines(); assert messages[:2]==NOTICES
            assert all(line.startswith('ws53-symc: unimplemented/unavailable') for line in messages[2:])
            assert bool(messages[2:])==(number in (2,3,6)),messages

        for writing in (False,True):
            for reset_kind in ('system','clear','clock'):
                with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
                    s=Symc(cmd); cmd('irq_intercept_in /machine/intc')
                    s.load(0,2,b'K'*16); s.configure(ch,0,2,1,1)
                    data=b'A'*16; s.descriptors(ch,data,bytes(16)); s.submit(ch)
                    s.step(3000 if writing else 1000)
                    prop='pending-writes' if writing else 'pending-reads'; assert s.get(prop)==1
                    s.fault(ch,8)
                    if reset_kind=='system': reset()
                    elif reset_kind=='clear': s.write(0x60,1<<ch); s.step(5000)
                    else: s.clock(False)
                    assert s.get(prop)==1
                    s.fault(ch,8,False); s.step(1000); assert s.get(prop)==0
                    if writing: assert s.bytes(s.addresses(ch)[1],16)==reference(2,b'K'*16,1,data,bytes(16))[0]
                    if reset_kind=='clock':
                        s.clock(True); s.step(100000); assert s.read(0xc000)&(1<<ch)
                    else:
                        assert s.get('writes-after-reset' if writing else 'reads-after-reset')==1
                        assert s.read(s.bank(ch,True)+0x34)==0 and not s.read(0xc000)
                    report['late_io']+=1
                assert diagnostics[0].splitlines()==NOTICES,diagnostics
    report['passed']=True
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); report=symc_test(str(args.qemu.resolve()))
    args.output.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report,indent=2))
