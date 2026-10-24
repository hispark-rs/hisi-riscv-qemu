#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Abstract CMD53/ADMA data and adversarial ownership tests; no board access."""
import argparse
from contextlib import contextmanager
import json
import struct
from ws53_gpio_test import running
from ws53_sdio_init_test import Sdio, PROFILE, RESET

TABLE, DATA = 0x20010000, 0x20020000
NOTICE = 'ws53-sdio: assumed CMD53-length-delimited ADMA/latched-status/ACK profile; no CIS, electrical or drain qualification'


class Peer(Sdio):
    def set(self, name, value):
        self.cmd.qmp('qom-set', dict(path='/machine/sdio', property=name, value=value))
    def memory(self, address, data):
        for start in range(0, len(data), 4096):
            b = data[start:start+4096]
            self.cmd(f'write {address+start:#x} {len(b)} 0x{b.hex()}')
    def bytes(self, address, count):
        return b''.join(bytes.fromhex(self.cmd(f'read {address+i:#x} {min(4096,count-i)}')[0].removeprefix('0x'))
                        for i in range(0,count,4096))
    def step(self, ns): self.cmd(f'clock_step {ns}')
    def ready(self):
        self.clocks(); self.input('host',0,True); self.configure(); self.input('host',1,True)
        self.write(0x1c,4); self.write(0x110,0x2000)
        self.write(0x11c,self.read(0x11c)|1)
        self.write(0x18,(self.read(0x18)&~7)|4)
    def request(self, write, length, payload=None):
        if length <= 512:
            count, block, mode = length & 511, 0, 0
        else:
            block = next(n for n in range(1,4096) if length % n == 0 and length//n <= 511)
            count, mode = length//block, 1
        argument = (int(write)<<31)|(1<<28)|(mode<<27)|count
        self.set('host-command',argument); self.set('host-block-size',block)
        if write: self.set('host-write-data',payload.hex())
        self.input('host',2,True)
        assert self.read(0x254)==argument and self.read(0x258)==block
        assert self.read(0x110)==(0x10 if write else 8)
        return argument
    def submit(self, write, length, pointer=TABLE):
        self.write(0x1c,4); self.write(0x110,0x10 if write else 8)
        if not write:
            self.write(0x120,(self.read(0x120)&~0x1fffff)|length)
        self.write(0x40,pointer)


@contextmanager
def peer(qemu):
    options = PROFILE + ('-global','ws53-sdio.assume-adma-profile=on',
                          '-global','ws53-sdio.config-seed=0xa55a5aa5')
    with running(qemu,False,options) as (cmd,irq,reset,diagnostics):
        s=Peer(cmd); cmd('irq_intercept_in /machine/intc'); s.ready()
        yield s,irq,reset
        rejected=s.count('rejections')
    lines=diagnostics[0].splitlines()
    assert lines[0]==NOTICE and len(lines)==1+rejected,diagnostics


def desc(address,length,flags=0x21): return struct.pack('<HHI',flags,length,address)
def pattern(length): return bytes((i*37+19)&255 for i in range(length))


def sdio_adma_test(qemu):
    normal=0
    for write in (False,True):
        for length in (1,3,4,7,31,511,512,513,1024,65535,65536):
            with peer(qemu) as (s,irq,_):
                payload=pattern(length)
                cuts=[min(3,length)]
                if length>3: cuts += [min(length-3,65535)]
                if sum(cuts)<length: cuts += [length-sum(cuts)]
                offset=0; table=b''
                for count in cuts:
                    table += desc(DATA+offset,count); offset+=count
                # A poison entry after the requested byte count must never be
                # fetched. complete_send does not magically add an END bit.
                table += desc(0xffffffff,0,0xffff)
                s.memory(TABLE,table)
                s.memory(DATA-16,b'\xa5'*16+(b'\xa5'*length if write else payload)+b'\xa5'*16)
                if not write:
                    old_status=s.read(0x104)
                    s.write(0x120,(s.read(0x120)&~0x1fffff)|length)
                    s.write(0x104,old_status|1)
                    assert s.count('read-offer-pending') and s.count('read-offer-length')==length
                    assert s.count('read-notifications')==1 and s.read(0x104)==old_status|1
                    assert s.count('dma-bytes')==0 and not s.count('dma-active')
                s.request(write,length,payload)
                assert not s.count('read-offer-pending')
                s.submit(write,length)
                s.step(100000000)
                assert s.count('dma-transfers')==1 and s.count('dma-bytes')==length
                assert not s.count('dma-active') and not s.count('dma-posted-write')
                assert s.count('dma-errors')==s.count('rejections')==0
                assert s.read(0x110)==(0x21 if write else 0x22) and irq.get(46,False)
                if write: assert s.bytes(DATA,length)==payload
                else: assert bytes.fromhex(s.count('device-read-data'))==payload
                assert s.bytes(DATA-16,16)==s.bytes(DATA+length,16)==b'\xa5'*16
                assert s.bytes(TABLE,len(table))==table
                s.write(0x1c,4); s.write(0x110,0x3f); s.read(0x110)
                assert not irq.get(46,False)
                normal+=1
    faults=[]
    for fault in ('table-bus','data-bus','zero-length','end-flag','invalid-flag','table-exhausted',
                  'read-bus','write-bus','partial-error'):
        with peer(qemu) as (s,irq,_):
            write=fault!='read-bus'; length=130 if fault=='table-exhausted' else 8
            payload=pattern(length); s.memory(DATA,b'\xa5'*(length+16))
            table=desc(DATA,length)
            pointer=TABLE
            if fault=='table-bus': pointer=0x2005fffc
            elif fault=='data-bus': table=desc(0x2005fffc,length)
            elif fault=='zero-length': table=desc(DATA,0)
            elif fault=='end-flag': table=desc(DATA,length,0x23)
            elif fault=='invalid-flag': table=desc(DATA,length,0x20)
            elif fault=='table-exhausted': table=b''.join(desc(DATA+i,1) for i in range(129))
            elif fault=='read-bus': s.input('fault',2,True)
            elif fault=='write-bus': s.input('fault',3,True)
            elif fault=='partial-error': table=desc(DATA,4)+desc(DATA+4,0)
            s.memory(TABLE,table); s.request(write,length,payload); s.submit(write,length,pointer)
            s.step(100000000)
            assert s.count('dma-errors')==1 and s.count('dma-transfers')==0
            assert not s.count('dma-active') and not s.count('dma-posted-write')
            assert s.read(0x110)==(0x80 if write else 0x84) and irq.get(46,False)
            moved=129 if fault=='table-exhausted' else (4 if fault=='partial-error' else 0)
            assert s.count('dma-bytes')==moved
            if write: assert s.bytes(DATA,length+16)==payload[:moved]+b'\xa5'*(length+16-moved)
            assert s.count('rejections')==0
            faults.append(fault)
    with peer(qemu) as (s,irq,_):
        payload=pattern(16); s.memory(DATA,b'\xa5'*32); s.memory(TABLE,desc(DATA,16))
        s.request(True,16,payload); s.input('fault',4,True); s.submit(True,16)
        s.step(1000); assert s.count('dma-posted-write')
        s.write(0x18,s.read(0x18)&~7); s.gate(0,False); s.input('host',0,False)
        s.step(100000)
        assert not s.count('dma-posted-write') and s.count('dma-active')
        assert s.bytes(DATA,32)==payload[:4]+b'\xa5'*28
        assert not irq.get(46,False) and s.count('dma-transfers')==0
        s.input('fault',4,False); s.gate(0,True); s.input('host',0,True); s.step(100000)
        assert s.bytes(DATA,32)==payload+b'\xa5'*16 and s.count('dma-transfers')==1
        assert s.read(0x110)==0x21 and not irq.get(46,False)
        s.write(0x18,(s.read(0x18)&~7)|4); s.read(0x18); assert irq.get(46,False)
        assert s.count('rejections')==0
    with peer(qemu) as (s,irq,_):
        payload=pattern(16); s.memory(DATA,b'\xa5'*32); s.memory(TABLE,desc(DATA,16))
        s.request(True,16,payload); s.input('fault',4,True); s.submit(True,16)
        s.step(1000); assert s.count('dma-posted-write')
        reset=int(s.cmd(f'readl {RESET:#x}')[0],0)
        s.cmd(f'writel {RESET:#x} {reset&~2:#x}'); s.cmd(f'writel {RESET:#x} {reset|2:#x}')
        assert not s.count('dma-active') and s.count('dma-posted-write')
        s.step(100000)
        assert s.bytes(DATA,32)==payload[:4]+b'\xa5'*28
        assert s.count('dma-posted-after-reset')==1 and s.count('dma-transfers')==0
        assert s.read(0x110)==0 and not irq.get(46,False)
        s.input('fault',4,False); s.ready(); s.request(True,16,payload); s.submit(True,16); s.step(100000)
        assert s.bytes(DATA,32)==payload+b'\xa5'*16 and s.count('dma-transfers')==1
        assert s.count('rejections')==0
    with peer(qemu) as (s,_,_):
        s.memory(TABLE,desc(DATA,8)); s.memory(DATA,pattern(8))
        s.request(False,8); s.submit(False,8); s.input('fault',1,True); s.step(100000)
        assert s.count('dma-active') and s.count('dma-bytes')==0 and s.read(0x110)==0
        s.input('fault',5,True); s.input('fault',1,False); s.step(100000)
        assert s.count('dma-transfers')==1 and s.read(0x110)==0 and not s.count('dma-active')
        assert bytes.fromhex(s.count('device-read-data'))==pattern(8)
    rejected=[]
    for name,argument,block in (
            ('function-zero',8,0), ('function-two',0x20000008,0),
            ('incrementing',0x14000008,0), ('nonzero-address',0x10000208,0),
            ('zero-block-count',0x18000000,8), ('zero-block-size',0x18000001,0),
            ('oversize-block',0x18000001,4096), ('missing-write-data',0x90000008,0)):
        with peer(qemu) as (s,_,_):
            s.set('host-command',argument); s.set('host-block-size',block)
            s.input('host',2,True)
            assert s.count('rejections')==1 and s.read(0x110)==0
            assert not s.count('dma-active') and s.count('dma-bytes')==0
            rejected.append(name)
    for name in ('without-request','unacknowledged-start','unaligned-table','wrong-send-length'):
        with peer(qemu) as (s,_,_):
            if name!='without-request': s.request(False,8)
            if name not in ('without-request','unacknowledged-start'):
                s.write(0x1c,4); s.write(0x110,8)
            s.write(0x120,(s.read(0x120)&~0x1fffff)|(7 if name=='wrong-send-length' else 8))
            s.write(0x40,TABLE+(1 if name=='unaligned-table' else 0))
            assert s.count('rejections')==1 and not s.count('dma-active')
            assert s.count('dma-bytes')==0
            rejected.append(name)
    with peer(qemu) as (s,_,_):
        s.memory(TABLE,desc(DATA,8)); s.memory(DATA,b'\xa5'*16)
        s.request(True,8,pattern(8)); s.input('fault',6,True); s.submit(True,8)
        s.step(100000000)
        assert s.count('dma-posted-write') and s.count('dma-bytes')==0
        assert s.bytes(DATA,16)==b'\xa5'*16
        s.input('fault',6,False); s.step(100000)
        assert s.count('dma-transfers')==1 and s.bytes(DATA,16)==pattern(8)+b'\xa5'*8
    with peer(qemu) as (s,_,_):
        s.write(0x104,s.read(0x104)|1)
        assert s.count('rejections')==1 and s.count('read-notifications')==0
        s.write(0x120,(s.read(0x120)&~0x1fffff)|64)
        s.write(0x104,s.read(0x104)|1)
        assert s.count('read-offer-pending') and s.count('read-notifications')==1
        s.write(0x104,s.read(0x104)|1)
        assert s.count('rejections')==2 and s.count('read-notifications')==1
    with peer(qemu) as (s,_,reset):
        s.write(0x120,(s.read(0x120)&~0x1fffff)|64)
        s.write(0x104,s.read(0x104)|1)
        assert s.count('read-offer-pending') and s.count('read-notifications')==1
        reset()
        assert not s.count('read-offer-pending') and s.count('read-notifications')==0
        assert s.count('read-offer-length')==0
    return dict(normal_transfers=normal, dma_faults=faults, irq46_mask_and_ack=True,
                posted_write_survives_mask_clock_loss=True, posted_write_survives_ip_reset=True,
                reset_reinitialize=True, stall_resume=True, completion_suppression=True,
                rejected_requests_and_submissions=rejected, held_posted_write_release=True,
                read_offer_precedes_host_request=True, zero_duplicate_offer_rejected=True,
                offer_reset_cleared=True, send_command_self_clear_assumed=False,
                descriptor_flags='only audited 0x21', termination='assumed CMD53 byte count',
                hardware_accessed=False, cis_or_physical_drain_verified=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qemu',required=True)
    print(json.dumps(sdio_adma_test(parser.parse_args().qemu),indent=2))
