#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""WS53 MDMA memory behavior/IRQ/cancellation tests; no physical bus guarantees."""
import argparse
import json
import subprocess
from ws53_gpio_test import running

BASE=0x52070000
SRC=0x20020000
DST=0x20040000
PROFILE=('-global','ws53-mdma.assume-profile=on')
NOTICE='ws53-mdma: assumed DTCM mem-to-mem same-width posted-write profile'


class Bus:
    def __init__(self,cmd): self.cmd=cmd
    def read(self,off): return int(self.cmd(f'readl {BASE+off:#x}')[0],0)
    def write(self,off,val): self.cmd(f'writel {BASE+off:#x} {val:#x}')
    def step(self,ns): self.cmd(f'clock_step {ns}')
    def prop(self,name): return self.cmd.qmp('qom-get',{'path':'/machine/mdma','property':name})
    def fault(self,name,ch=0,on=True): self.cmd(f'set_irq_in /machine/mdma {name} {ch} {int(on)}')
    def reg(self,ch,off): return self.read(0x100+ch*32+off)
    def set(self,ch,off,val): self.write(0x100+ch*32+off,val)
    def mem(self,addr,length): return bytes.fromhex(self.cmd(f'read {addr:#x} {length:#x}')[0].removeprefix('0x'))
    def fill(self,addr,data): self.cmd(f'write {addr:#x} {len(data):#x} 0x{data.hex()}')
    def configure(self,ch=0,count=3,width=4,src=SRC,dst=DST,si=True,di=True,tc=True):
        encoding={1:0,2:1,4:2}[width]
        self.set(ch,0,0); self.set(ch,0x10,src); self.set(ch,4,dst)
        self.set(ch,0x14,count|(encoding<<18)|(encoding<<21)|(int(si)<<26)|(int(di)<<27)|(int(tc)<<31))
    def enable(self,ch=0,mask=0x3000): self.set(ch,8,mask|1)


def clean(diagnostics): assert diagnostics[0].splitlines()==[NOTICE],diagnostics


def mdma_test(qemu):
    combinations=0
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd); cmd('irq_intercept_in /machine/intc')
        for ch in range(8):
            for width in (1,2,4):
                for si,di in ((False,False),(False,True),(True,False),(True,True)):
                    reset(); b.write(0x1c,1); data=bytes((i*37+ch*11)&255 for i in range(68))
                    b.fill(SRC,data); b.fill(DST-4,b'\xa5'*76)
                    b.configure(ch,17,width,si=si,di=di); b.enable(ch)
                    assert b.read(0x10)==1<<ch and not irq.get(58,False)
                    b.step(499); assert b.prop('reads')==0
                    b.step(1); assert b.prop('reads')==1 and b.reg(ch,8)&0x8000
                    assert b.mem(DST,68)==b'\xa5'*68
                    b.step(499); assert b.prop('writes')==0
                    b.step(1); assert b.prop('writes')==1
                    b.step(16000)
                    expected=bytearray(b'\xa5'*68)
                    for i in range(17):
                        source=i*width if si else 0; dest=i*width if di else 0
                        expected[dest:dest+width]=data[source:source+width]
                    assert b.mem(DST-4,76)==b'\xa5'*4+expected+b'\xa5'*4
                    assert b.read(0x10)==0 and b.reg(ch,0x14)&0xfff==0
                    assert b.reg(ch,0x10)==SRC+(17*width if si else 0)
                    assert b.reg(ch,4)==DST+(17*width if di else 0)
                    assert b.read(4)==(1<<ch)|(1<<(ch+8)) and b.read(0xc)==1<<ch and irq[58]
                    assert b.prop('transfers')==1 and b.prop('writes')==17
                    b.write(8,0); assert irq[58]
                    b.write(8,1<<ch); assert b.read(4)==b.read(0xc)==0 and not irq[58]
                    combinations+=1
        # All channels independently active, mask polarity, shared IRQ retention.
        reset(); b.write(0x1c,1)
        for ch in range(8):
            b.fill(SRC+ch*16,bytes([ch+1])*16); b.fill(DST+ch*16,b'\x00'*16)
            b.configure(ch,4,4,SRC+ch*16,DST+ch*16); b.enable(ch,mask=0)
        assert b.read(0x10)==255
        b.step(4000); assert b.prop('transfers')==8 and b.prop('writes')==32
        assert b.read(0xc)==255 and b.read(4)==0 and not irq[58]
        for ch in range(8):
            assert b.mem(DST+ch*16,16)==bytes([ch+1])*16
            b.set(ch,8,0x2000)
        assert irq[58] and b.read(4)==0xffff
        b.write(8,0x55); assert irq[58] and b.read(0xc)==0xaa
        b.write(8,0xaa); assert not irq[58]
        # Terminal interrupt control gates raw TC, not transfer progress.
        reset(); b.write(0x1c,1); b.configure(tc=False); b.enable(); b.step(3000)
        assert b.prop('transfers')==1 and b.read(0xc)==0 and not irq[58]
        # Full12-bit element count, both aperture ends, actual byte comparison.
        reset(); b.write(0x1c,1); length=4095*4
        data=bytes((i*17+23)&255 for i in range(length)); b.fill(0x20010000,data)
        b.fill(0x20060000-length,b'\x00'*length)
        b.configure(count=4095,src=0x20010000,dst=0x20060000-length); b.enable(); b.step(4095000)
        assert b.mem(0x20060000-length,length)==data and b.prop('writes')==4095
    clean(diagnostics)
    with running(qemu,False,PROFILE) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd); cmd('irq_intercept_in /machine/intc')
        for action in ('halt','disable','controller-disable'):
            reset(); b.write(0x1c,1); b.fill(SRC,bytes(range(12))); b.fill(DST,b'\xff'*12)
            b.configure(); b.enable(); b.step(600)
            assert b.prop('pending-writes')==1
            if action=='halt': b.set(0,8,0x13001)
            elif action=='disable': b.set(0,8,0x3000)
            else: b.write(0x1c,0)
            b.step(399); assert b.mem(DST,12)==b'\xff'*12
            b.step(1); assert b.mem(DST,12)==bytes(range(4))+b'\xff'*8
            b.step(100000); assert b.prop('writes')==1 and b.prop('pending-writes')==0
            if action=='controller-disable': b.write(0x1c,1)
            else: b.enable()
            b.step(2000); assert b.mem(DST,12)==bytes(range(12)) and b.prop('writes')==3
        # Halt before first read cancels no data; restore exact remaining phase.
        reset(); b.write(0x1c,1); b.configure(); b.enable(); b.step(123)
        b.set(0,8,0x13001); b.step(100000); assert b.prop('reads')==0
        b.set(0,8,0x3001); b.step(376); assert b.prop('reads')==0
        b.step(1); assert b.prop('reads')==1
        # Clock, per-channel stall and bus stall preserve pending write time.
        for name in ('clock','stall','bus-stall'):
            reset(); b.write(0x1c,1); b.configure(); b.enable(); b.step(623)
            b.fault(name,on=name!='clock'); b.step(100000); assert b.prop('writes')==0
            b.fault(name,on=name=='clock'); b.step(376); assert b.prop('writes')==0
            b.step(1); assert b.prop('writes')==1
        # Reset cancels pending work in the model; faults persist across reset.
        b.fault('bus-stall'); reset(); b.write(0x1c,1); b.configure(); b.enable(); b.step(100000)
        assert b.prop('pending-writes')==1 and b.prop('writes')==0
        reset(); b.fault('bus-stall',on=False); b.step(100000)
        assert b.prop('pending-writes')==0 and b.prop('writes')==0 and b.read(0x10)==0
        # Read/write failures do not report success, modify destination or other channels.
        for name in ('read-error','write-error'):
            reset(); b.write(0x1c,1); b.fill(DST,b'\x55'*16)
            b.configure(0,1); b.configure(7,1,src=SRC+4,dst=DST+8)
            b.fault(name); b.enable(0,mask=0); b.enable(7); b.step(1000)
            assert b.prop('errors')==1 and b.prop('transfers')==1 and b.read(0x10)==0
            assert b.mem(DST,4)==b'\x55'*4 and b.mem(DST+8,4)==b.mem(SRC+4,4)
            assert b.read(0xc)==0x180 and b.read(4)==0x8080
            b.set(0,8,0x1000); assert b.read(4)==0x18081 and irq[58]
            b.write(8,0x80); assert b.read(4)==0x10001 and irq[58]
            b.write(8,0x100); assert b.read(4)==0 and not irq[58]
            b.fault(name,on=False); b.configure(0,1); b.enable(); b.step(1000)
            assert b.prop('transfers')==2 and b.mem(DST,4)==b.mem(SRC,4)
    clean(diagnostics)
    invalid=0
    with running(qemu,False,PROFILE) as (cmd,_,reset,diagnostics):
        b=Bus(cmd)
        commands=['readb 0x52070004','readw 0x52070004','readq 0x52070004','readl 0x52070005',
                  'readl 0x52070000','readl 0x52070008','readl 0x52070014','readl 0x52070018',
                  'readl 0x52070020','readl 0x5207010c','readl 0x52070118','readl 0x5207011c',
                  'writew 0x5207001c 1','writel 0x52070004 1','writel 0x5207000c 1',
                  'writel 0x52070010 1','writel 0x52070008 0x10000','writel 0x5207001c 3',
                  'writel 0x52070014 1','writel 0x52070018 1','writel 0x52070020 1',
                  'writel 0x52070100 4','writel 0x52070108 0x200','writel 0x52070108 0x4000',
                  'writel 0x52070114 0x1001','writel 0x52070114 0x6c0001',
                  'writel 0x52070114 0x200001','writel 0x52070114 0x1000001']
        for command in commands: cmd(command); invalid+=1
        for src,dst,count in ((SRC,DST,0),(SRC+1,DST,1),(SRC,DST+2,1),
                              (0x2000fffc,DST,1),(SRC,0x2005fffc,2),(SRC,0xfffffffc,1)):
            reset(); b.write(0x1c,1); b.configure(src=src,dst=dst,count=count); b.enable(); invalid+=1
            assert b.read(0x10)==0 and b.prop('pending-writes')==0
        reset(); b.write(0x1c,1); b.configure(count=1); b.enable(); b.step(600)
        b.set(0,0x10,SRC+4); b.set(0,0x14,0); invalid+=2
        b.set(0,8,0x3000); b.enable(); invalid+=1 # Cannot rearm an outstanding disabled beat.
        assert b.reg(0,0x10)==SRC and b.reg(0,0x14)&0xfff==1
        b.step(400); assert b.prop('writes')==1
    lines=diagnostics[0].splitlines()
    assert lines[0]==NOTICE and len(lines)==invalid+1,diagnostics
    assert all('unimplemented' in line for line in lines[1:]),diagnostics
    with running(qemu,False) as (cmd,_,_,diagnostics):
        b=Bus(cmd); b.write(0x1c,1); b.configure(); b.enable(); b.step(100000)
        assert b.prop('writes')==0 and b.read(0x10)==0
    assert diagnostics[0].splitlines()==['ws53-mdma: unimplemented transfer without assume-profile'],diagnostics
    for value in (0,1000000001):
        result=subprocess.run([qemu,'-M','ws53','-accel','qtest','-display','none','-monitor','none',
            '-serial','null','-global',f'ws53-mdma.phase-ns={value}'],capture_output=True,text=True,timeout=5)
        assert result.returncode!=0 and 'phase-ns must be' in result.stderr,result
    return {'channel_width_increment_combinations':combinations,'parallel_channels':8,
            'maximum_elements':4095,'width_bytes':[1,2,4],'irq':58,'invalid_diagnostics':invalid,
            'posted_write_survives_halt_disable':True,'read_write_errors_and_recovery':True,
            'exact_clock_pauses':True,'peripheral_requests_implemented':False,
            'linked_lists_implemented':False,'cache_or_hardware_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--qemu',required=True)
    print(json.dumps(mdma_test(p.parse_args().qemu),indent=2))
