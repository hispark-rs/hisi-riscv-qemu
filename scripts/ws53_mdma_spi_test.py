#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""SPI0/QSPI1 request-paced byte/word MDMA, full-duplex FIFO and cancellation."""
import argparse
import json
import subprocess
from ws53_gpio_test import running
from ws53_mdma_test import Bus, SRC, DST, PROFILE, NOTICE
from ws53_spi_test import Bus as Spi, PROFILE as SPI_PROFILE, NOTICE as SPI_NOTICE

ARGS = (*PROFILE, *SPI_PROFILE, '-global', 'ws53-mdma.assume-spi=on')
DMA_NOTICE = 'ws53-mdma: assumed SPI0/QSPI1 same-width requests, flow1/2, master0/prot2'
NOTICES = [SPI_NOTICE, NOTICE, DMA_NOTICE]


def configure(b, spi, ch, count, width, rx, address=None):
    data = spi.base+spi.map['fifo']; address = address if address is not None else DST if rx else SRC
    b.configure(ch, count, width, data if rx else address, address if rx else data, not rx, rx)
    b.set(ch, 0x14, b.reg(ch,0x14)|(2<<28))
    request = (4 if spi.index else 6)+int(rx)
    cfg = 0x3001|((2 if rx else 1)<<9)|(request<<(1 if rx else 5))
    b.set(ch, 8, cfg)
    return cfg


def requests(spi, tx=4, rx=0, enable=3):
    spi.write(0x50 if spi.index else 0x20, tx)
    spi.write(0x54 if spi.index else 0x1c, rx)
    spi.write('dma', enable)


def spi_dma_test(qemu):
    combinations = 0
    with running(qemu, False, ARGS) as (cmd, irq, reset, diagnostics):
        b = Bus(cmd); cmd('irq_intercept_in /machine/intc')
        def setup(port=0, width=1):
            reset(); b.write(0x1c,1); spi=Spi(cmd,port); spi.init(width*8); requests(spi); return spi
        for port in range(2):
            for width in (1,4):
                for ch in range(8):
                    spi=setup(port,width); other=(ch+1)%8; count=33
                    data=bytes((i*19+port*11+ch*7)&255 for i in range(count*width))
                    b.fill(SRC,data); b.fill(DST-4,b'\x5a'*(count*width+8))
                    configure(b,spi,other,count,width,True); configure(b,spi,ch,count,width,False)
                    b.step(499); assert b.prop('reads')==0
                    b.step(1); assert b.prop('reads')==1 and b.prop('pending-writes')==1
                    b.step(200000)
                    assert b.prop('transfers')==2 and b.prop('writes')==count*2
                    assert b.mem(SRC,len(data))==data
                    assert b.mem(DST-4,len(data)+8)==b'\x5a'*4+bytes(x^0xa5 for x in data)+b'\x5a'*4
                    assert spi.frames()==count and spi.read('tx_level')==spi.read('rx_level')==0
                    assert b.reg(ch,4)==b.reg(other,0x10)==spi.base+spi.map['fifo']
                    assert b.reg(ch,0x10)==SRC+len(data) and b.reg(other,4)==DST+len(data)
                    assert b.reg(ch,0x14)&0xfff==b.reg(other,0x14)&0xfff==0
                    assert b.read(0x10)==0 and b.read(0xc)==(1<<ch)|(1<<other) and irq[58]
                    b.write(8,1<<ch); assert irq[58]
                    b.write(8,1<<other); assert not irq[58]
                    combinations+=1
        # Both ports and widths concurrently, beyond FIFO depth, no cross-routing.
        reset(); b.write(0x1c,1)
        for port,width in ((0,1),(1,4)):
            spi=Spi(cmd,port);spi.init(width*8);requests(spi)
            data=bytes((i*23+port*31)&255 for i in range(257*width))
            b.fill(SRC+port*2048,data);b.fill(DST+port*2048,b'\x00'*len(data))
            configure(b,spi,port*2+1,257,width,True,DST+port*2048)
            configure(b,spi,port*2,257,width,False,SRC+port*2048)
        b.step(2000000); assert b.prop('transfers')==4 and b.prop('writes')==1028
        for port,width in ((0,1),(1,4)):
            assert b.mem(DST+port*2048,257*width)==bytes(((i*23+port*31)&255)^0xa5 for i in range(257*width))
        for port in range(2):
            # DMA completion only means FIFO acceptance. Clock stop preserves
            # the remaining frame time and suppresses new peripheral requests.
            spi=setup(port);b.fill(SRC,b'A');configure(b,spi,0,1,1,False)
            b.step(1000);assert b.prop('transfers')==1 and spi.frames()==0 and spi.read('tx_level')==1
            b.step(400);spi.fault('clock',False);b.step(10000)
            assert spi.frames()==0 and spi.read('tx_level')==1
            spi.fault('clock',True);b.step(599);assert spi.frames()==0
            b.step(1);assert spi.frames()==1 and spi.read('tx_level')==0
            # Empty RX waits. Threshold is > RX level, so a short tail waits
            # until software lowers the threshold; no fabricated zero reads.
            spi=setup(port); requests(spi,rx=4); configure(b,spi,1,3,1,True)
            b.step(10000); assert b.prop('reads')==0
            for x in range(3):spi.write('fifo',x)
            b.step(10000);assert spi.read('rx_level')==3 and b.prop('reads')==0
            requests(spi);b.step(3000);assert b.mem(DST,3)==bytes(x^0xa5 for x in range(3))
            # TX threshold pacing holds data in FIFO; TC precedes wire completion.
            spi=setup(port);spi.fault('stretch',True);b.fill(SRC,bytes(range(17)))
            configure(b,spi,0,17,1,False);b.step(20000)
            assert b.prop('writes')==5 and spi.read('tx_level')==5 and spi.frames()==0
            requests(spi,tx=7);b.step(10000)
            assert b.prop('writes')==8 and spi.read('tx_level')==8
            # Disabled requests stop new reads without resetting FIFO.
            requests(spi,enable=0);spi.fault('stretch',False);b.step(10000)
            assert b.prop('writes')==8 and spi.frames()==8
            requests(spi);b.step(20000);assert b.prop('transfers')==1 and spi.frames()==17
            assert spi.read('raw')&spi.bits(8) # RX overflow is reported, not fake success.
            # Clock withdrawal before a pending TX write rejects that beat.
            spi=setup(port);b.fill(SRC,b'A');b.fault('bus-stall');cfg=configure(b,spi,0,1,1,False)
            b.step(500);assert b.prop('pending-writes')==1
            spi.fault('clock',False);b.fault('bus-stall',on=False);b.step(500)
            assert b.prop('errors')==1 and b.prop('writes')==0
            spi.fault('clock',True)
            # A posted write survives channel halt and request-disable.
            spi=setup(port);b.fault('bus-stall');configure(b,spi,0,1,1,False);b.step(500)
            b.set(0,8,(cfg&~1)|0x10000);requests(spi,enable=0);b.fault('bus-stall',on=False);b.step(500)
            assert b.prop('writes')==1 and spi.read('tx_level')==1 and b.read(0x10)==0
            b.step(1000);assert spi.frames()==1
            # A consumed RX word survives channel cancel and SPI clock removal.
            spi=setup(port);spi.write('fifo',0x13);b.step(1000);b.fill(DST,b'\x5a')
            b.fault('bus-stall',1);cfg=configure(b,spi,1,1,1,True);b.step(500)
            assert spi.read('rx_level')==0 and b.mem(DST,1)==b'\x5a'
            b.set(1,8,(cfg&~1)|0x10000);spi.fault('clock',False)
            b.fault('bus-stall',1,False);b.step(500);assert b.mem(DST,1)==b'\xb6'
            spi.fault('clock',True)
            # RX overflow/collision and bus errors remain independently observable.
            for rx in (False,True):
                for fault in ('read-error','write-error'):
                    spi=setup(port);b.fill(SRC,b'B')
                    if rx:spi.write('fifo',0x12);b.step(1000)
                    b.fault(fault);configure(b,spi,0,1,1,rx);b.step(1000)
                    assert b.prop('errors')==1 and b.prop('writes')==0 and b.read(0xc)==0x100
                    b.fault(fault,on=False)
            # Reset removes all scheduled DMA/serial work; injected faults persist.
            spi=setup(port);b.fault('bus-stall');configure(b,spi,0,1,1,False);b.step(500)
            reset();b.fault('bus-stall',on=False);b.step(10000)
            assert b.prop('pending-writes')==b.prop('writes')==spi.frames()==0
    assert diagnostics[0].splitlines()==NOTICES,diagnostics
    rejects=0
    with running(qemu,False,ARGS) as (cmd,irq,reset,diagnostics):
        b=Bus(cmd)
        for port in range(2):
            for kind in ('wrong-data','wrong-route','wrong-direction','halfword','width-mismatch','prot0','increment','zero','too-large'):
                reset();b.write(0x1c,1);spi=Spi(cmd,port);spi.init();requests(spi)
                data=spi.base+spi.map['fifo']; width=2 if kind=='halfword' else 4 if kind=='width-mismatch' else 1
                count=0 if kind=='zero' else 4096 if kind=='too-large' else 1
                b.configure(0,count,width,SRC,data+4 if kind=='wrong-data' else data,True,kind=='increment')
                b.set(0,0x14,b.reg(0,0x14)|(0 if kind=='prot0' else 2<<28))
                request=(4 if port else 6)+(1 if kind=='wrong-direction' else 0)
                if kind=='wrong-route':request=6 if port else 4
                b.set(0,8,0x3201|(request<<5));assert b.read(0x10)==0
                # Count4096 is rejected both at CONTROL write and channel arm.
                rejects+=2 if kind=='too-large' else 1
            for off,value in ((0x50 if port else 0x20,8),(0x54 if port else 0x1c,8),(spi.map['dma'],4)):
                spi.write(off,value);rejects+=1
    lines=diagnostics[0].splitlines();assert lines[:3]==NOTICES and len(lines)==3+rejects,lines
    assert all('unimplemented write' in s for s in lines[3:])
    bad=subprocess.run([qemu,'-M','ws53','-display','none','-global','ws53-mdma.assume-spi=on'],capture_output=True,text=True,timeout=5)
    assert bad.returncode!=0 and 'assume-spi requires assume-profile' in bad.stderr
    return {'duplex_port_channel_width_combinations':combinations,'ports_concurrent_frames_each':257,
            'rejected_configs':rejects,'checks':['data and guards','request thresholds and short RX tail',
                'FIFO pressure and overflow','separate DMA/serial completion','clock removal','pending TX/RX after cancel',
                'read/write bus errors','IRQ58 isolation','reset'],
            'hardware_verified':False,'physical_request_cache_or_drain_verified':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--qemu',required=True)
    print(json.dumps(spi_dma_test(p.parse_args().qemu),indent=2))
